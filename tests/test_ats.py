import pytest

from portalfinder.ats import ats_tenant, clean_ats_url, detect_ats, is_asset, is_not_portal
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


@pytest.mark.parametrize("url,clean", [
    ("https://accenture.wd103.myworkdayjobs.com/AccentureCareers/login?redirect=%2FAccentureCareers%2FuserHome",
     "https://accenture.wd103.myworkdayjobs.com/AccentureCareers"),
    ("https://acme.wd5.myworkdayjobs.com/en-US/External/job/Berlin/Engineer_R1",
     "https://acme.wd5.myworkdayjobs.com/en-US/External"),
    ("https://career5.successfactors.eu/career?company=lidlstiftuP2&lang=de_DE&navBarLevel=MY_PROFILE",
     "https://career5.successfactors.eu/career?company=lidlstiftuP2"),
    ("https://jobs.lever.co/acme/1234/apply", "https://jobs.lever.co/acme"),
    ("https://boards.greenhouse.io/embed/job_board?for=stripe", "https://boards.greenhouse.io/stripe"),
    ("https://acme-career.talent-soft.com/mon-compte/cv.aspx", "https://acme-career.talent-soft.com/"),
    ("https://www.acme.com/careers", "https://www.acme.com/careers"),
])
def test_clean_ats_url(url, clean):
    assert clean_ats_url(url) == clean


def test_assets_and_non_portal_links():
    assert is_asset("https://cdn.phenompeople.com/x/bluebird.min-1.0.js")
    assert is_asset("https://cdn.x.com/eeo.PDF")
    assert not is_asset("https://www.acme.com/careers")
    assert is_not_portal("https://www.tata.com/careers/jobs/jobdetails?jobId=1&location=India")
    assert is_not_portal("https://www.amazon.jobs/applicant/dashboard/applications")
    assert is_not_portal("https://passport.amazon.jobs/accountInfo")
    assert not is_not_portal("https://careers.dhl.com/global/en/dhl-usa")
    assert not is_not_portal("https://www.accenture.com/ar-es/careers")
