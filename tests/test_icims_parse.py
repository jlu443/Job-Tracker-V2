from src import icims_scraper

# Trimmed from a real portal: the post date precedes the title link in each row.
_PAGE = '''
<div class="row"><div class="col-xs-6 header left"><span class="sr-only field-label">Job Locations</span>
<span > US-VA-Herndon</span></div><div class="col-xs-6 header right">
<span title="9/25/2026 4:19 PM"> 1 day ago</span></div>
<div class="col-xs-12 title"><a href="https://careers-x.icims.com/jobs/171326/intern/job?in_iframe=1"
class="iCIMS_Anchor" title="171326 - Intern"><h3 > Summer 2027 Cybersecurity Intern</h3></a></div></div>
<div class="row"><div class="col-xs-6 header right"><span title="9/9/2026 1:00 PM">x</span></div>
<div class="col-xs-12 title"><a href="https://careers-x.icims.com/jobs/170320/ds/job?in_iframe=1"
class="iCIMS_Anchor" title="170320 - DS"><h3 > Data Science Intern</h3></a></div>
<dl><dt class="iCIMS_JobHeaderField"><span class="sr-only field-label">Job Location</span> </dt>
<dd class="iCIMS_JobHeaderData"><span > US-MN-Bloomington</span> </dd></dl></div>
'''


def test_parse_listing_keeps_each_rows_own_date_and_location():
    rows = icims_scraper.parse_listing(_PAGE, "careers-x.icims.com", "X")
    assert [(r.job_id, r.title, r.location, r.posted_on) for r in rows] == [
        ("icims_careers-x_171326", "Summer 2027 Cybersecurity Intern", "Herndon, VA, US", "2026-09-25"),
        ("icims_careers-x_170320", "Data Science Intern", "Bloomington, MN, US", "2026-09-09"),
    ]
