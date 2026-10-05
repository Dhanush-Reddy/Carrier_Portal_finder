"""A stand-in LinkedIn MCP server for the tests (stdio)."""

import json

from mcp.server.fastmcp import FastMCP

server = FastMCP("fake-linkedin")

ABOUT = {
    "wipro": (
        "Wipro\nAbout us\nOverview\nLeading technology services company.\n"
        "Website\nhttps://www.wipro.com\nIndustry\nIT Services and IT Consulting\n"
        "Company size\n10,001+ employees\n241,000 associated members\n"
        "Headquarters\nBengaluru, Karnataka\nFounded\n1945\n"),
    "zoho": json.dumps({
        "name": "Zoho", "industry": {"name": "Software Development"},
        "website": "https://www.zoho.com", "company_size": "10,001+ employees",
        "headquarters": {"city": "Chennai", "geographicArea": "Tamil Nadu"},
        "url": "https://www.linkedin.com/company/zoho/"}),
    "tiny-shop": "Website\nhttps://tiny.example\nIndustry\nRetail\nCompany size\n2-10 employees\n",
}


# The shape stickerdaniel/linkedin-mcp-server returns: raw page text per section.
STICKERDANIEL = {
    "tcs": {
        "url": "https://www.linkedin.com/company/tata-consultancy-services/",
        "sections": {"about": (
            "Overview\nA global leader in IT services.\nWebsite\nhttp://www.tcs.com\n"
            "External link for Tata Consultancy Services\nIndustry\nIT Services and IT Consulting\n"
            "Company size\n10,001+ employees\n601,546 associated members\n"
            "Headquarters\nMumbai, Maharashtra\nFounded\n1968")},
        "references": {"about": [{"kind": "company_urn", "value": "1353"}]},
    },
    "soft-limited": {
        "url": "https://www.linkedin.com/company/soft-limited/",
        "sections": {},
        "section_errors": {"about": {
            "error_type": "rate_limit",
            "error_message": "[Rate limited] LinkedIn blocked this section. Try again later."}},
    },
}


@server.tool()
def get_company_profile(company_name: str, sections: str | None = None) -> dict | str:
    """Company profile by LinkedIn slug."""
    if company_name == "throttled":
        raise RuntimeError("LinkedIn rate limit reached, try later")
    if company_name in STICKERDANIEL:
        return STICKERDANIEL[company_name]
    return ABOUT.get(company_name, "This page doesn't exist")


@server.tool()
def search_jobs(keywords: str) -> str:
    """Search jobs."""
    return "[]"


if __name__ == "__main__":
    server.run()
