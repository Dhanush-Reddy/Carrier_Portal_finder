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


@server.tool()
def get_company_profile(company_name: str) -> str:
    """Company profile by LinkedIn slug."""
    if company_name == "throttled":
        raise RuntimeError("LinkedIn rate limit reached, try later")
    return ABOUT.get(company_name, "This page doesn't exist")


@server.tool()
def search_jobs(keywords: str) -> str:
    """Search jobs."""
    return "[]"


if __name__ == "__main__":
    server.run()
