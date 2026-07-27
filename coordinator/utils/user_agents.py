"""
User Agent Management
Centralized list of user agents for anti-detection and browser-specific content handling
"""

# Desktop user agents only (for consistent content rendering)
# Keep this list synchronized with desktop-agent/lib/anti_detection.py
USER_AGENTS = [
    # Chrome on Windows
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',

    # Chrome on macOS
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',

    # Firefox on Windows
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0',

    # Firefox on macOS
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:121.0) Gecko/20100101 Firefox/121.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:120.0) Gecko/20100101 Firefox/120.0',

    # Safari on macOS
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15',

    # Edge on Windows
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0',

    # Chrome on Linux
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
]


def get_user_agent_by_index(index: int) -> str:
    """
    Get user agent by index (for consistent assignment to agents)

    Args:
        index: Index into USER_AGENTS list

    Returns:
        User agent string

    Raises:
        ValueError: If index is out of range
    """
    if index < 0 or index >= len(USER_AGENTS):
        raise ValueError(f"User agent index {index} out of range (0-{len(USER_AGENTS)-1})")

    return USER_AGENTS[index]


def get_user_agent_index(user_agent: str) -> int:
    """
    Get index of user agent in the list

    Args:
        user_agent: User agent string

    Returns:
        Index in USER_AGENTS list, or -1 if not found
    """
    try:
        return USER_AGENTS.index(user_agent)
    except ValueError:
        return -1


def get_total_user_agents() -> int:
    """Get total number of user agents in the pool"""
    return len(USER_AGENTS)
