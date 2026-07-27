"""
Tests for Agent State Manager

Tests Redis-based state tracking and incremental update detection.
"""

import pytest
from unittest.mock import Mock, AsyncMock, patch
from sqlalchemy.ext.asyncio import AsyncSession
from services.agent_state_manager import AgentStateManager
from models.target import Target
from models.target_selector import TargetSelector


@pytest.fixture
def mock_redis():
    """Create mock Redis client"""
    redis = AsyncMock()
    redis.hgetall = AsyncMock(return_value={})
    redis.hset = AsyncMock()
    redis.delete = AsyncMock()
    redis.expire = AsyncMock()
    redis.incr = AsyncMock(return_value=1)
    redis.pipeline = Mock(return_value=MockPipeline())
    return redis


class MockPipeline:
    """Mock Redis pipeline"""
    def __init__(self):
        self.commands = []

    def delete(self, key):
        self.commands.append(('delete', key))
        return self

    def hset(self, key, mapping=None):
        self.commands.append(('hset', key, mapping))
        return self

    def expire(self, key, seconds):
        self.commands.append(('expire', key, seconds))
        return self

    async def execute(self):
        return [True] * len(self.commands)


@pytest.fixture
def db():
    """Create mock database session"""
    return AsyncMock(spec=AsyncSession)


@pytest.fixture
def manager(db, mock_redis):
    """Create state manager with mocked dependencies"""
    return AgentStateManager(db, mock_redis)


@pytest.mark.asyncio
async def test_first_sync_returns_none(manager, mock_redis):
    """First sync should return None (full rebuild)"""
    # Redis returns empty state (first sync)
    mock_redis.hgetall.return_value = {}

    targets = [
        Target(
            id=1,
            url="https://example.com",
            selector=".content",
            check_period_ms=10000,
            check_type="content",
            enabled=True,
            baseline_hash="abc123"
        )
    ]

    result = await manager.compute_incremental_update("agent-1", targets)

    assert result is None  # First sync = full rebuild


@pytest.mark.asyncio
async def test_no_changes_returns_empty_update(manager, mock_redis):
    """No changes should return empty incremental update"""
    # Setup: existing state
    target = Target(
        id=1,
        url="https://example.com",
        selector=".content",
        check_period_ms=10000,
        check_type="content",
        enabled=True,
        baseline_hash="abc123"
    )

    # Redis returns existing state with same hash
    existing_hash = manager._hash_target(target)
    mock_redis.hgetall.return_value = {
        b'1': existing_hash.encode()
    }

    result = await manager.compute_incremental_update("agent-1", [target])

    assert result is not None
    assert len(result['changes']) == 0
    assert result['full_rebuild_required'] is False


@pytest.mark.asyncio
async def test_add_target_incremental(manager, mock_redis):
    """Adding a target should return incremental update"""
    # Existing state: target 1, stored under its REAL hash so it reads as unchanged
    # (the stored value must equal _hash_target, not an arbitrary placeholder).
    target1 = Target(
        id=1,
        url="https://example1.com",
        selector=".content",
        check_period_ms=10000,
        check_type="content",
        enabled=True,
        baseline_hash="hash1"
    )
    mock_redis.hgetall.return_value = {
        b'1': manager._hash_target(target1).encode()
    }

    # New state: target 1 (unchanged) + target 2 (added)
    target2 = Target(
        id=2,
        url="https://example2.com",
        selector=".content",
        check_period_ms=10000,
        check_type="content",
        enabled=True,
        baseline_hash="hash2"
    )

    mock_redis.incr.return_value = 1

    result = await manager.compute_incremental_update("agent-1", [target1, target2])

    assert result is not None
    assert len(result['changes']) == 1
    assert result['changes'][0]['operation'] == 'add'
    assert result['changes'][0]['target_id'] == 2
    assert result['version'] == 1


@pytest.mark.asyncio
async def test_remove_target_incremental(manager, mock_redis):
    """Removing a target should return incremental update"""
    # New state keeps target 1 (stored under its REAL hash, so it reads as unchanged)
    # and drops target 2.
    target1 = Target(
        id=1,
        url="https://example1.com",
        selector=".content",
        check_period_ms=10000,
        check_type="content",
        enabled=True,
        baseline_hash="hash1"
    )
    mock_redis.hgetall.return_value = {
        b'1': manager._hash_target(target1).encode(),
        b'2': b'hash2'
    }

    mock_redis.incr.return_value = 2

    result = await manager.compute_incremental_update("agent-1", [target1])

    assert result is not None
    assert len(result['changes']) == 1
    assert result['changes'][0]['operation'] == 'remove'
    assert result['changes'][0]['target_id'] == 2
    assert result['version'] == 2


@pytest.mark.asyncio
async def test_modify_target_incremental(manager, mock_redis):
    """Modifying a target should return incremental update"""
    # _hash_target keys off url/check_period_ms/check_type/enabled/selectors (NOT the
    # scalar `selector` column), so vary a hashed field to register the modification.
    target_old = Target(
        id=1,
        url="https://example.com",
        selector=".content",
        check_period_ms=10000,  # old interval
        check_type="content",
        enabled=True,
        baseline_hash="abc123"
    )

    # Setup: existing state
    old_hash = manager._hash_target(target_old)
    mock_redis.hgetall.return_value = {
        b'1': old_hash.encode()
    }

    # New state: modified check interval -> hash changes -> detected as modify
    target_new = Target(
        id=1,
        url="https://example.com",
        selector=".content",
        check_period_ms=20000,  # new interval
        check_type="content",
        enabled=True,
        baseline_hash="abc123"
    )

    mock_redis.incr.return_value = 3

    result = await manager.compute_incremental_update("agent-1", [target_new])

    assert result is not None
    assert len(result['changes']) == 1
    assert result['changes'][0]['operation'] == 'modify'
    assert result['changes'][0]['target_id'] == 1
    assert result['version'] == 3


@pytest.mark.asyncio
async def test_too_many_changes_triggers_full_rebuild(manager, mock_redis):
    """Too many changes (>10%) should trigger full rebuild"""
    # Setup: existing state with 10 targets
    existing_state = {
        f'{i}'.encode(): f'hash{i}'.encode()
        for i in range(10)
    }
    mock_redis.hgetall.return_value = existing_state

    # New state: 5 targets removed, 5 targets added (50% change)
    targets = [
        Target(
            id=i + 10,
            url=f"https://example{i}.com",
            selector=".content",
            check_period_ms=10000,
            check_type="content",
            enabled=True,
            baseline_hash=f"newhash{i}"
        )
        for i in range(10)
    ]

    result = await manager.compute_incremental_update("agent-1", targets)

    # Should return None (full rebuild)
    assert result is None


@pytest.mark.asyncio
async def test_hash_target_consistency(manager):
    """Test that identical targets produce identical hashes"""
    target1 = Target(
        id=1,
        url="https://example.com",
        selector=".content",
        check_period_ms=10000,
        check_type="content",
        enabled=True,
        baseline_hash="abc123"
    )

    target2 = Target(
        id=1,
        url="https://example.com",
        selector=".content",
        check_period_ms=10000,
        check_type="content",
        enabled=True,
        baseline_hash="abc123"
    )

    hash1 = manager._hash_target(target1)
    hash2 = manager._hash_target(target2)

    assert hash1 == hash2


@pytest.mark.asyncio
async def test_hash_target_detects_changes(manager):
    """Test that hash changes when a hashed field changes"""
    # The hash covers url/check_period_ms/check_type/enabled/selectors (not the scalar
    # `selector` column), so vary check_period_ms to prove change detection.
    target1 = Target(
        id=1,
        url="https://example.com",
        selector=".content",
        check_period_ms=10000,
        check_type="content",
        enabled=True,
        baseline_hash="abc123"
    )

    target2 = Target(
        id=1,
        url="https://example.com",
        selector=".content",
        check_period_ms=20000,  # Different interval
        check_type="content",
        enabled=True,
        baseline_hash="abc123"
    )

    hash1 = manager._hash_target(target1)
    hash2 = manager._hash_target(target2)

    assert hash1 != hash2


@pytest.mark.asyncio
async def test_serialize_target(manager):
    """Test target serialization for agent (selector data lives under `selectors`)."""
    target = Target(
        id=1,
        url="https://example.com",
        check_period_ms=10000,
        check_type="content",
        enabled=True,
    )
    # Selector-level fields (selector/ignore_regex/baseline_*) now serialize from the
    # TargetSelector relationship, not scalar Target columns.
    target.selectors = [
        TargetSelector(
            id=7,
            name="main",
            selector=".content",
            content_type="text",
            ignore_regex="ignore.*",
            baseline_hash="abc123",
            baseline_content="test content",
            priority=0,
            enabled=True,
        )
    ]

    serialized = manager._serialize_target(target)

    assert serialized['id'] == 1
    assert serialized['url'] == "https://example.com"
    assert serialized['check_period_ms'] == 10000
    assert serialized['check_type'] == "content"
    assert serialized['enabled'] is True

    assert len(serialized['selectors']) == 1
    sel = serialized['selectors'][0]
    assert sel['selector'] == ".content"
    assert sel['content_type'] == "text"
    assert sel['ignore_regex'] == "ignore.*"
    assert sel['baseline_hash'] == "abc123"
    assert sel['baseline_content'] == "test content"


@pytest.mark.asyncio
async def test_version_increment_atomic(manager, mock_redis):
    """Test version increment is atomic"""
    mock_redis.incr.side_effect = [1, 2, 3, 4, 5]

    versions = []
    for _ in range(5):
        version = await manager._get_next_version()
        versions.append(version)

    assert versions == [1, 2, 3, 4, 5]
    assert mock_redis.incr.call_count == 5
    assert mock_redis.incr.call_args[0][0] == 'agent_update_version'


@pytest.mark.asyncio
async def test_clear_agent_state(manager, mock_redis):
    """Test clearing agent state"""
    await manager.clear_agent_state("agent-1")

    mock_redis.delete.assert_called_once_with("agent_state:agent-1")


@pytest.mark.asyncio
async def test_redis_failure_fallback(manager, mock_redis):
    """Test graceful fallback when Redis fails"""
    # Simulate Redis failure
    mock_redis.hgetall.side_effect = Exception("Redis connection failed")

    targets = [
        Target(
            id=1,
            url="https://example.com",
            selector=".content",
            check_period_ms=10000,
            check_type="content",
            enabled=True,
            baseline_hash="abc123"
        )
    ]

    # Should not crash, should return None (full rebuild)
    result = await manager.compute_incremental_update("agent-1", targets)

    # Falls back to empty state, triggers full rebuild
    assert result is None


@pytest.mark.asyncio
async def test_multiple_changes_in_one_update(manager, mock_redis):
    """Test multiple operations in single update"""
    # Setup: existing state
    mock_redis.hgetall.return_value = {
        b'1': b'hash1_old',  # Will be modified
        b'2': b'hash2'       # Will be removed
    }

    # New state: target 1 modified, target 2 removed, target 3 added
    targets = [
        Target(
            id=1,
            url="https://example1.com",
            selector=".new",  # Modified
            check_period_ms=10000,
            check_type="content",
            enabled=True,
            baseline_hash="abc123"
        ),
        Target(
            id=3,
            url="https://example3.com",  # Added
            selector=".content",
            check_period_ms=10000,
            check_type="content",
            enabled=True,
            baseline_hash="def456"
        )
    ]

    mock_redis.incr.return_value = 1

    result = await manager.compute_incremental_update("agent-1", targets)

    assert result is not None
    assert len(result['changes']) == 3

    operations = {c['operation'] for c in result['changes']}
    assert 'add' in operations
    assert 'remove' in operations
    assert 'modify' in operations


@pytest.mark.asyncio
async def test_manager_without_redis(db):
    """Test manager works without Redis (database fallback)"""
    manager = AgentStateManager(db, redis_client=None)

    assert manager._use_redis is False

    # Should still work (using database)
    targets = [
        Target(
            id=1,
            url="https://example.com",
            selector=".content",
            check_period_ms=10000,
            check_type="content",
            enabled=True,
            baseline_hash="abc123"
        )
    ]

    # DB fallback: `await db.execute(...)` must resolve to a SYNC result whose
    # scalar_one_or_none() returns the agent row. Assigning a plain Mock() as the
    # AsyncMock's return_value keeps scalar_one_or_none SYNC — leaving it auto-mocked
    # hands back an unawaited coroutine (the old failure at agent_state_manager.py:204).
    # No agent row yet -> empty prior state -> first sync -> full rebuild (None), and
    # the no-row path also skips the ORM flag_modified write.
    mock_result = Mock()
    mock_result.scalar_one_or_none.return_value = None
    db.execute.return_value = mock_result

    result = await manager.compute_incremental_update("agent-1", targets)

    # First sync = full rebuild
    assert result is None
