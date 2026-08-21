"""ATS adapters for job fetching."""

from .base import CareerFetcher
from .detector import ATSDetector
from .greenhouse import GreenhouseFetcher
from .lever import LeverFetcher
from .ashby import AshbyFetcher
from .workday import WorkdayFetcher
from .eightfold import EightfoldFetcher
from .successfactors import SuccessFactorsFetcher
from .workable import WorkableFetcher
from .smartrecruiters import SmartRecruitersFetcher
from .oracle import OracleFetcher
from .recruitee import RecruiteeFetcher
from .personio import PersonioFetcher
from .bamboohr import BambooHRFetcher
from .talentbrew import TalentBrewFetcher
from .apple import AppleFetcher
from .generic import GenericFetcher
from .uber import UberFetcher
from .amazon import AmazonFetcher
from .meta import MetaFetcher
from .google import GoogleFetcher
from .tiktok import TikTokFetcher
from .salesforce import SalesforceFetcher
from .phenom import PhenomFetcher

__all__ = [
    "CareerFetcher",
    "ATSDetector",
    "GreenhouseFetcher",
    "LeverFetcher",
    "AshbyFetcher",
    "WorkdayFetcher",
    "EightfoldFetcher",
    "SuccessFactorsFetcher",
    "WorkableFetcher",
    "SmartRecruitersFetcher",
    "OracleFetcher",
    "RecruiteeFetcher",
    "PersonioFetcher",
    "BambooHRFetcher",
    "TalentBrewFetcher",
    "AppleFetcher",
    "GenericFetcher",
    "UberFetcher",
    "AmazonFetcher",
    "MetaFetcher",
    "GoogleFetcher",
    "TikTokFetcher",
    "SalesforceFetcher",
    "PhenomFetcher",
]
