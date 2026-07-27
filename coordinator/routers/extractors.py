"""
Extractors API - CRUD operations for selector extractors.
"""
import logging
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_

from database import get_db
from models.selector_extractor import SelectorExtractor
from models.target_selector import TargetSelector
from security.api_key import get_current_api_key
from security.validation import InputValidator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/extractors", tags=["Extractors"])


def _validate_extractor_config(extract_type: str, config: Optional[Dict[str, Any]]) -> None:
    """Validate type-specific extractor config before persisting.

    For regex extractors this rejects ReDoS-prone patterns up front (length /
    nesting / structural backtracking heuristic + a timeout-bounded probe) so a
    catastrophic pattern can never be stored and later executed against scraped
    content. Raises HTTPException(400) on an unsafe/invalid pattern.
    """
    if extract_type == "regex" and config:
        pattern = config.get("pattern")
        if pattern:
            # Raises HTTPException(400) if the pattern is too long/complex,
            # uncompilable, or matches the ReDoS structural/timeout heuristics.
            InputValidator.validate_regex(pattern)


# Pydantic models
class ExtractorCreate(BaseModel):
    """Create a new extractor."""
    target_selector_id: int = Field(..., description="Selector this extractor belongs to")
    name: str = Field(..., min_length=1, max_length=100)
    output_name: str = Field(..., min_length=1, max_length=100, description="Key name in extracted data")
    enabled: bool = True
    extract_type: str = Field(default="text", description="Type: text, attribute, regex, css, json_path, links")
    config: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Type-specific configuration")
    is_array: bool = Field(default=False, description="Return array of all matches")
    default_value: Optional[str] = Field(None, description="Default if extraction fails")


class ExtractorUpdate(BaseModel):
    """Update an existing extractor."""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    output_name: Optional[str] = Field(None, min_length=1, max_length=100)
    enabled: Optional[bool] = None
    extract_type: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    is_array: Optional[bool] = None
    default_value: Optional[str] = None


class ExtractorResponse(BaseModel):
    """Extractor response model."""
    id: int
    target_selector_id: int
    name: str
    output_name: str
    enabled: bool
    extract_type: str
    config: Optional[Dict[str, Any]]
    is_array: bool
    default_value: Optional[str]

    class Config:
        from_attributes = True


class TestExtractorRequest(BaseModel):
    """Request to test an extractor."""
    content: str = Field(..., description="Content to extract from")
    content_type: str = Field(default="text", description="Content type: text or html")


class TestExtractorResponse(BaseModel):
    """Response from testing an extractor."""
    success: bool
    value: Any
    error: Optional[str] = None


# Endpoints
@router.get(
    "/selector/{selector_id}",
    response_model=List[ExtractorResponse],
    summary="List Extractors",
    description="Get all extractors for a selector.",
)
async def list_extractors(
    selector_id: int,
    enabled_only: bool = False,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """List all extractors for a selector."""
    from security.ownership import verify_selector_ownership
    selector = await verify_selector_ownership(db, selector_id)

    query = select(SelectorExtractor).where(SelectorExtractor.target_selector_id == selector_id)

    if enabled_only:
        query = query.where(SelectorExtractor.enabled == True)

    result = await db.execute(query)
    extractors = result.scalars().all()

    return [
        ExtractorResponse(
            id=e.id,
            target_selector_id=e.target_selector_id,
            name=e.name,
            output_name=e.output_name,
            enabled=e.enabled,
            extract_type=e.extract_type,
            config=e.config,
            is_array=e.is_array,
            default_value=e.default_value,
        )
        for e in extractors
    ]


@router.post(
    "",
    response_model=ExtractorResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Extractor",
    description="Create a new extractor for a selector.",
)
async def create_extractor(
    request: ExtractorCreate,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Create a new extractor."""
    from security.ownership import verify_selector_ownership
    selector = await verify_selector_ownership(db, request.target_selector_id)

    # Validate extract_type
    valid_types = ["text", "attribute", "regex", "css", "json_path", "links"]
    if request.extract_type not in valid_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid extract_type. Must be one of: {valid_types}",
        )

    # ReDoS guard: reject unsafe regex patterns before storing.
    _validate_extractor_config(request.extract_type, request.config)

    extractor = SelectorExtractor(
        target_selector_id=request.target_selector_id,
        name=request.name,
        output_name=request.output_name,
        enabled=request.enabled,
        extract_type=request.extract_type,
        config=request.config,
        is_array=request.is_array,
        default_value=request.default_value,
    )

    db.add(extractor)
    await db.commit()
    await db.refresh(extractor)

    logger.info(f"Created extractor {extractor.id} ({extractor.name}) for selector {request.target_selector_id}")

    return ExtractorResponse(
        id=extractor.id,
        target_selector_id=extractor.target_selector_id,
        name=extractor.name,
        output_name=extractor.output_name,
        enabled=extractor.enabled,
        extract_type=extractor.extract_type,
        config=extractor.config,
        is_array=extractor.is_array,
        default_value=extractor.default_value,
    )


@router.get(
    "/{extractor_id}",
    response_model=ExtractorResponse,
    summary="Get Extractor",
    description="Get a specific extractor by ID.",
)
async def get_extractor(
    extractor_id: int,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Get an extractor by ID."""
    from security.ownership import verify_selector_ownership

    result = await db.execute(
        select(SelectorExtractor).where(SelectorExtractor.id == extractor_id)
    )
    extractor = result.scalar_one_or_none()
    if not extractor:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Extractor not found")

    # Verify ownership via selector → target → owner
    await verify_selector_ownership(db, extractor.target_selector_id)

    return ExtractorResponse(
        id=extractor.id,
        target_selector_id=extractor.target_selector_id,
        name=extractor.name,
        output_name=extractor.output_name,
        enabled=extractor.enabled,
        extract_type=extractor.extract_type,
        config=extractor.config,
        is_array=extractor.is_array,
        default_value=extractor.default_value,
    )


@router.patch(
    "/{extractor_id}",
    response_model=ExtractorResponse,
    summary="Update Extractor",
    description="Update an existing extractor.",
)
async def update_extractor(
    extractor_id: int,
    request: ExtractorUpdate,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Update an extractor."""
    from security.ownership import verify_selector_ownership

    result = await db.execute(
        select(SelectorExtractor).where(SelectorExtractor.id == extractor_id)
    )
    extractor = result.scalar_one_or_none()

    if not extractor:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Extractor not found")

    await verify_selector_ownership(db, extractor.target_selector_id)

    # Update fields if provided
    if request.name is not None:
        extractor.name = request.name
    if request.output_name is not None:
        extractor.output_name = request.output_name
    if request.enabled is not None:
        extractor.enabled = request.enabled
    if request.extract_type is not None:
        valid_types = ["text", "attribute", "regex", "css", "json_path", "links"]
        if request.extract_type not in valid_types:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid extract_type. Must be one of: {valid_types}",
            )
        extractor.extract_type = request.extract_type
    if request.config is not None:
        extractor.config = request.config

    # ReDoS guard: validate the EFFECTIVE (post-update) type + config so a regex
    # pattern can't be slipped in via either field on update.
    if request.extract_type is not None or request.config is not None:
        _validate_extractor_config(extractor.extract_type, extractor.config)

    if request.is_array is not None:
        extractor.is_array = request.is_array
    if request.default_value is not None:
        extractor.default_value = request.default_value

    await db.commit()
    await db.refresh(extractor)

    logger.info(f"Updated extractor {extractor_id}")

    return ExtractorResponse(
        id=extractor.id,
        target_selector_id=extractor.target_selector_id,
        name=extractor.name,
        output_name=extractor.output_name,
        enabled=extractor.enabled,
        extract_type=extractor.extract_type,
        config=extractor.config,
        is_array=extractor.is_array,
        default_value=extractor.default_value,
    )


@router.delete(
    "/{extractor_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete Extractor",
    description="Delete an extractor.",
)
async def delete_extractor(
    extractor_id: int,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Delete an extractor."""
    from security.ownership import verify_selector_ownership

    result = await db.execute(
        select(SelectorExtractor).where(SelectorExtractor.id == extractor_id)
    )
    extractor = result.scalar_one_or_none()
    if not extractor:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Extractor not found")

    await verify_selector_ownership(db, extractor.target_selector_id)
    await db.delete(extractor)
    await db.commit()

    logger.info(f"Deleted extractor {extractor_id}")


@router.post(
    "/{extractor_id}/test",
    response_model=TestExtractorResponse,
    summary="Test Extractor",
    description="Test an extractor with sample content.",
)
async def test_extractor(
    extractor_id: int,
    request: TestExtractorRequest,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Test an extractor with sample content."""
    from security.ownership import verify_selector_ownership

    result = await db.execute(
        select(SelectorExtractor).where(SelectorExtractor.id == extractor_id)
    )
    extractor = result.scalar_one_or_none()
    if not extractor:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Extractor not found")

    await verify_selector_ownership(db, extractor.target_selector_id)

    from services.extractor import extractor_service

    try:
        extracted = extractor_service.extract_all(
            content=request.content,
            extractors=[extractor.to_dict()],
            content_type=request.content_type,
        )

        value = extracted.get(extractor.output_name)

        return TestExtractorResponse(
            success=True,
            value=value,
        )

    except Exception as e:
        logger.error(f"Error testing extractor {extractor_id}: {e}")
        return TestExtractorResponse(
            success=False,
            value=None,
            error=str(e),
        )


@router.patch(
    "/{extractor_id}/toggle",
    response_model=ExtractorResponse,
    summary="Toggle Extractor",
    description="Toggle an extractor's enabled status.",
)
async def toggle_extractor(
    extractor_id: int,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Toggle an extractor's enabled status."""
    from security.ownership import verify_selector_ownership

    result = await db.execute(
        select(SelectorExtractor).where(SelectorExtractor.id == extractor_id)
    )
    extractor = result.scalar_one_or_none()

    if not extractor:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Extractor {extractor_id} not found",
        )

    # Verify ownership via selector → target → owner before mutating.
    await verify_selector_ownership(db, extractor.target_selector_id)

    extractor.enabled = not extractor.enabled
    await db.commit()
    await db.refresh(extractor)

    logger.info(f"Toggled extractor {extractor_id} to enabled={extractor.enabled}")

    return ExtractorResponse(
        id=extractor.id,
        target_selector_id=extractor.target_selector_id,
        name=extractor.name,
        output_name=extractor.output_name,
        enabled=extractor.enabled,
        extract_type=extractor.extract_type,
        config=extractor.config,
        is_array=extractor.is_array,
        default_value=extractor.default_value,
    )


@router.post(
    "/test-content",
    summary="Test Extract Content",
    description="Test extraction on content without saving an extractor.",
)
async def test_extract_content(
    content: str,
    content_type: str = "text",
    extract_type: str = "text",
    config: Optional[Dict[str, Any]] = None,
    is_array: bool = False,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Test extraction on content without saving."""
    from services.extractor import extractor_service

    extractor_config = {
        "name": "test",
        "output_name": "result",
        "enabled": True,
        "extract_type": extract_type,
        "config": config or {},
        "is_array": is_array,
    }

    try:
        extracted = extractor_service.extract_all(
            content=content,
            extractors=[extractor_config],
            content_type=content_type,
        )

        return {
            "success": True,
            "result": extracted.get("result"),
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }
