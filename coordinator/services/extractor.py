"""
Extractor service - extracts structured data from HTML/text content.

Runs extractors defined on selectors and returns extracted data
that can be used in trigger conditions and passed to actions.
"""
import re
import logging
from typing import Dict, Any, List, Optional
from bs4 import BeautifulSoup

from security.validation import InputValidator

logger = logging.getLogger(__name__)


class ExtractorService:
    """
    Runs extractors on content and returns structured data.
    """

    def extract_all(
        self,
        content: str,
        extractors: List[Dict[str, Any]],
        content_type: str = 'text'
    ) -> Dict[str, Any]:
        """
        Run all extractors on content and return extracted data.

        Args:
            content: The content to extract from (text or HTML)
            extractors: List of extractor configs
            content_type: 'text' or 'html'

        Returns:
            Dictionary of {output_name: extracted_value}
        """
        extracted = {}

        # Parse HTML if needed
        soup = None
        if content_type == 'html':
            soup = BeautifulSoup(content, 'lxml')

        for extractor in extractors:
            if not extractor.get('enabled', True):
                continue

            try:
                output_name = extractor['output_name']
                extract_type = extractor.get('extract_type', 'text')
                config = extractor.get('config', {})
                is_array = extractor.get('is_array', False)
                default_value = extractor.get('default_value')

                value = self._extract_single(
                    content=content,
                    soup=soup,
                    extract_type=extract_type,
                    config=config,
                    is_array=is_array,
                    content_type=content_type
                )

                if value is None and default_value is not None:
                    value = default_value

                extracted[output_name] = value

            except Exception as e:
                logger.warning(f"Extractor '{extractor.get('name', 'unknown')}' failed: {e}")
                extracted[extractor.get('output_name', 'unknown')] = extractor.get('default_value')

        return extracted

    def _extract_single(
        self,
        content: str,
        soup: Optional[BeautifulSoup],
        extract_type: str,
        config: Dict[str, Any],
        is_array: bool,
        content_type: str
    ) -> Any:
        """
        Extract a single value based on extractor type.
        """
        if extract_type == 'text':
            # Just return the text content
            if soup:
                return soup.get_text(separator=' ', strip=True)
            return content.strip()

        elif extract_type == 'attribute':
            # Extract HTML attribute
            if not soup:
                return None
            attr_name = config.get('attribute', 'href')
            if is_array:
                elements = soup.find_all(attrs={attr_name: True})
                return [el.get(attr_name) for el in elements]
            else:
                element = soup.find(attrs={attr_name: True})
                return element.get(attr_name) if element else None

        elif extract_type == 'regex':
            # Extract using regex.
            #
            # ReDoS defense: user-supplied patterns are executed under a hard
            # wall-clock timeout with a capped input length via
            # InputValidator.safe_regex_* (validate=True re-validates the pattern
            # defensively here too, in case it was persisted before validation
            # existed). On timeout the helpers fail-closed (no match / empty),
            # which falls through to the extractor's default_value.
            pattern = config.get('pattern')
            if not pattern:
                return None
            group = config.get('group', 1)

            if is_array:
                matches = InputValidator.safe_regex_findall(pattern, content)
                if matches and isinstance(matches[0], tuple):
                    # Multiple groups - return the specified group from each match
                    return [m[group - 1] if len(m) >= group else m[0] for m in matches]
                return matches
            else:
                match = InputValidator.safe_regex_search(pattern, content)
                if match:
                    if match.groups():
                        return match.group(group) if len(match.groups()) >= group else match.group(1)
                    return match.group(0)
                return None

        elif extract_type == 'css':
            # Extract using sub-CSS selector
            if not soup:
                return None
            sub_selector = config.get('sub_selector')
            if not sub_selector:
                return None

            attr_name = config.get('attribute')  # Optional: get attribute instead of text

            if is_array:
                elements = soup.select(sub_selector)
                if attr_name:
                    return [el.get(attr_name) for el in elements if el.get(attr_name)]
                return [el.get_text(strip=True) for el in elements]
            else:
                element = soup.select_one(sub_selector)
                if element:
                    if attr_name:
                        return element.get(attr_name)
                    return element.get_text(strip=True)
                return None

        elif extract_type == 'json_path':
            # Extract from JSON content
            import json
            try:
                data = json.loads(content)
                path = config.get('path', '$')
                # Simple JSON path support ($.key.subkey or $.array[0])
                return self._json_path_extract(data, path)
            except (json.JSONDecodeError, KeyError, IndexError):
                return None

        elif extract_type == 'links':
            # Extract all links (convenience extractor)
            if not soup:
                return None
            links = soup.find_all('a', href=True)
            result = []
            for link in links:
                result.append({
                    'url': link.get('href'),
                    'text': link.get_text(strip=True),
                })
            return result if is_array else (result[0] if result else None)

        else:
            logger.warning(f"Unknown extract_type: {extract_type}")
            return None

    def _json_path_extract(self, data: Any, path: str) -> Any:
        """
        Simple JSON path extraction (not full JSONPath, just basic dot/bracket notation).
        Example: $.items[0].price
        """
        if path.startswith('$'):
            path = path[1:]
        if path.startswith('.'):
            path = path[1:]

        if not path:
            return data

        # Split by dots, handling brackets
        parts = re.split(r'\.(?![^\[]*\])', path)

        current = data
        for part in parts:
            if not part:
                continue

            # Handle array access like items[0]
            bracket_match = re.match(r'(\w+)\[(\d+)\]', part)
            if bracket_match:
                key = bracket_match.group(1)
                index = int(bracket_match.group(2))
                if isinstance(current, dict):
                    current = current.get(key, [])
                if isinstance(current, list) and len(current) > index:
                    current = current[index]
                else:
                    return None
            else:
                if isinstance(current, dict):
                    current = current.get(part)
                else:
                    return None

            if current is None:
                return None

        return current


# Singleton instance
extractor_service = ExtractorService()
