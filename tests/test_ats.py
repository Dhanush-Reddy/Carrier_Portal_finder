import pytest

from portalfinder.ats import ats_tenant, detect_ats
from portalfinder.normalize import linkedin_company_url, website_domain


@pytest.mark.parametrize("url,provider", [
    ("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite", "Workday"),
    ("https://boards.greenhouse.io/stripe", "Greenhouse"),
    ("https://job-boards.greenhouse.io/anthropic", "Greenhouse"),
    ("https://jobs.lever.co/netflix", "Lever"),
    ("https://career5.successfactors.eu/career?company=x", "SuccessFactors"),
    ("https://careers-acme.icims.com/jobs", "iCIMS"),
    ("https://acme.taleo.net/careersection/2/jobsearch.ftl", "Oracle/Taleo"),
    ("https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX", "Oracle Recruiting Cloud"),
    ("https://jobs.smartrecruiters.com/Visa", "SmartRecruiters"),
    ("https://www.apple.com/careers/us/", None),
    ("https://evilmyworkdayjobs.com.example.org/x", None),
])
def test_detect_ats(url, provider):
    assert detect_ats(url) == provider


def test_normalisers():
    assert website_domain("https://WWW.Apple.com/uk/") == "apple.com"
    assert website_domain("abc.xyz") == "abc.xyz"
    assert linkedin_company_url("https://www.linkedin.com/company/Google/about/") == \
        "https://www.linkedin.com/company/google"
    assert linkedin_company_url("1441") == "https://www.linkedin.com/company/1441"
    assert linkedin_company_url(None) is None


def test_ats_tenant():
    assert ats_tenant("https://boards.greenhouse.io/acme/jobs/1") == "Greenhouse:acme"
    assert ats_tenant("https://jobs.lever.co/acme") == ats_tenant("https://jobs.lever.co/acme/x")
    assert ats_tenant("https://acme.wd5.myworkdayjobs.com/External") == \
        ats_tenant("https://acme.wd5.myworkdayjobs.com/en-US/External/job/1")
    assert ats_tenant("https://www.acme.com/careers") is None
