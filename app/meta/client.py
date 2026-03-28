"""
Meta Ads API client initialization.
Each call initializes the SDK with the tenant's access token.
"""
from facebook_business.api import FacebookAdsApi
from facebook_business.adobjects.adaccount import AdAccount
from facebook_business.adobjects.campaign import Campaign
from facebook_business.adobjects.adset import AdSet
from facebook_business.exceptions import FacebookRequestError
from app.config import settings


def init_meta_api(access_token: str) -> None:
    """Initialize the Facebook Ads SDK with a given access token."""
    FacebookAdsApi.init(
        app_id=settings.meta_app_id or None,
        app_secret=settings.meta_app_secret or None,
        access_token=access_token,
    )


def get_ad_account(access_token: str, ad_account_id: str) -> AdAccount:
    """Return an AdAccount object ready to use."""
    init_meta_api(access_token)
    # Ensure the ID has the 'act_' prefix
    if not ad_account_id.startswith("act_"):
        ad_account_id = f"act_{ad_account_id}"
    return AdAccount(ad_account_id)
