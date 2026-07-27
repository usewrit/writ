"""
Utility functions for parsing diff snippets.
"""
import re
import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def parse_diff_snippet(diff_snippet: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Parse a unified diff snippet to extract before and after content.

    Args:
        diff_snippet: Unified diff string (e.g., "--- +++ @@ -1 +1 @@-old content+new content")

    Returns:
        Tuple of (content_before, content_after)
    """
    if not diff_snippet:
        return None, None

    try:
        # Remove the unified diff header
        # Format can be:
        # 1. Multi-line: "---\n+++\n@@ -X +Y @@\n-old\n+new"
        # 2. Single-line: "--- +++ @@ -1 +1 @@-old content+new content"

        # Find the diff header pattern: @@ -X +Y @@
        # Use regex to find this specific pattern, not just any @@
        import re
        header_pattern = r'@@\s*-\d+(?:,\d+)?\s+\+\d+(?:,\d+)?\s*@@'
        match = re.search(header_pattern, diff_snippet)

        if not match:
            return None, None

        # Get the content after the header
        header_end = match.end()
        content = diff_snippet[header_end:].strip()

        if not content:
            return None, None

        # Split into lines (handles both \n separated and single line)
        lines = content.split('\n')

        content_before_parts = []
        content_after_parts = []

        for line in lines:
            # Parse each line - lines can contain multiple changes
            i = 0
            while i < len(line):
                if line[i] == '-':
                    # Find the next + or end of line
                    end = i + 1
                    while end < len(line) and line[end] not in ['+', '-']:
                        end += 1
                    content_before_parts.append(line[i+1:end])
                    i = end
                elif line[i] == '+':
                    # Find the next - or end of line
                    end = i + 1
                    while end < len(line) and line[end] not in ['+', '-']:
                        end += 1
                    content_after_parts.append(line[i+1:end])
                    i = end
                else:
                    # Context line (unchanged)
                    content_before_parts.append(line[i])
                    content_after_parts.append(line[i])
                    i += 1

        content_before = '\n'.join(content_before_parts) if content_before_parts else None
        content_after = '\n'.join(content_after_parts) if content_after_parts else None

        return content_before, content_after

    except Exception as e:
        # If parsing fails, return None
        logger.error(f"Error parsing diff snippet: {e}")
        return None, None


def format_diff_for_display(content_before: Optional[str], content_after: Optional[str]) -> dict:
    """
    Format before/after content for frontend display.

    Args:
        content_before: Content before the change
        content_after: Content after the change

    Returns:
        Dict with formatted content
    """
    return {
        "before": content_before or "N/A",
        "after": content_after or "N/A",
        "has_change": content_before != content_after
    }
