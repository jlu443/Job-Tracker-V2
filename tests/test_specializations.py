"""Specializations sheet: one field per job."""
import pytest

from src import classify, specializations


@pytest.mark.parametrize("title,field", [
    ("Software Engineer Intern", "tech"),
    ("ASIC Design Engineer New Grad", "tech"),
    ("Product Management Intern", "tech"),
    ("Robotics Engineer, R&D Engineering Development Program", "tech"),
    ("UX Design Research Intern", "tech"),
    ("2027 Quantitative Developer Intern", "tech"),
    ("Data Science Undergraduate Intern", "data_ai"),
    ("Machine Learning Engineer Intern", "data_ai"),
    ("Junior Data Analyst", "data_ai"),
    ("Cybersecurity Engineer I", "cyber"),
    ("Security Engineer Intern", "cyber"),
    ("Identity and Access Management Intern", "cyber"),
    ("SOC Analyst I", "cyber"),
    ("Security Officer - Night Shift", ""),
    ("New Graduate Nurse", "healthcare"),
    ("Pharmacy Intern", "healthcare"),
    ("Medical Laboratory Scientist I", "healthcare"),
    ("Summer 2027 Environmental, Health & Safety (EHS) Intern", ""),
    ("Associate Scientist, Molecular Function", "science"),
    ("Research Technician I", "science"),
    ("Chemist Intern", "science"),
    ("Research Intern", "science"),
    ("Finance Intern - Summer 2027", "business"),
    ("2027 Investment Banking Summer Analyst", "business"),
    ("Summer 2027 Quantitative Research Intern", "business"),
    ("Marketing Intern - Hybrid", "business"),
    ("Human Resources Intern (January 2027)", "business"),
    ("Market Research Intern", "business"),
    ("Fraud Analyst I", "business"),
    ("Sales Engineer Intern", ""),
    ("Civil Engineer Intern", ""),
    ("Mechanical Engineer Intern - New Product Development", ""),
])
def test_specialization(title, field):
    assert specializations.specialization(title, classify.categorize(title)) == field
