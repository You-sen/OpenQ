# """
# salesAssistant_schema.py
# ------------------------
# Pydantic models for the sales assistant.

# The frontend controls the entire product catalog — backend just receives
# whatever JSON the frontend sends and passes it to the AI.

# Two modes:
#   1. CATALOG mode   — user hasn't selected anything yet (timer expired)
#                       frontend sends full catalog + all discounts/promos
#   2. SELECTION mode — user selected one or more items
#                       frontend sends only the selected items + applicable offers
# """

# from pydantic import BaseModel
# from typing import Optional, List, Any


# # ============================================================
# #  Product / Service models
# #  (mirrors whatever structure frontend sends — kept flexible)
# # ============================================================

# class SubService(BaseModel):
#     """
#     A specific plan/tier under a service category.
#     Example: "Ultimate Pro Internet — $79.99/mo — Up to 1Gbps"
#     """
#     id: str                              # e.g. "internet_ultimate_pro"
#     name: str                            # e.g. "Ultimate Pro"
#     description: Optional[str] = None   # e.g. "Up to 1Gbps, unlimited data"
#     price: Optional[float] = None       # monthly price
#     price_label: Optional[str] = None   # e.g. "$79.99/mo" or "from $49"
#     features: Optional[List[str]] = []  # bullet points frontend wants AI to reference
#     tag: Optional[str] = None           # e.g. "Most Popular", "Best Value"


# class Discount(BaseModel):
#     """
#     A discount or promo that may apply.
#     Frontend decides eligibility — just sends relevant ones.
#     """
#     id: str                              # e.g. "promo_3month"
#     label: str                           # e.g. "3-Month Bundle Discount"
#     description: Optional[str] = None   # e.g. "Save 15% when you prepay 3 months"
#     condition: Optional[str] = None     # e.g. "Age 35+", "First-time customer"
#     value: Optional[str] = None         # e.g. "15% off" or "$10/mo off"


# class Service(BaseModel):
#     """
#     A top-level service category with its available sub-services.
#     Example: Internet → [Home Connection, Ultimate Pro, ...]
#     """
#     id: Optional[str]                              # e.g. "internet"
#     name: Optional[str]                            # e.g. "Internet"
#     icon: Optional[str] = None          # optional, ignored by AI
#     sub_services: Optional[List[SubService]] = []
#     applicable_discounts: Optional[List[Discount]] = []


# # ============================================================
# #  Request models
# # ============================================================

# class SalesMessage(BaseModel):
#     """
#     Sent on every chat turn.

#     mode:
#       "catalog"   — user hasn't selected anything; AI should explore and suggest
#       "selection" — user selected specific items; AI should affirm and upsell

#     history: last N messages for short-term context (frontend manages this)
#     """
#     message: str                              # the user's typed message
#     mode: str = "catalog"                     # "catalog" | "selection"

#     # Catalog mode: full list of all available services
#     catalog: Optional[List[Service]] = None

#     # Selection mode: only what the user picked
#     selected_services: Optional[List[SubService]] = []
#     selected_discounts: Optional[List[Discount]] = []

#     # Short conversation history — frontend sends last 6 turns max
#     history: Optional[List[dict]] = []        # [{"role": "user"|"assistant", "content": "..."}]

#     # Optional: any extra context frontend wants to pass (e.g. user age for discount hints)
#     context: Optional[dict] = None            # e.g. {"user_age": 38, "is_new_customer": True}


# # ============================================================
# #  Response model
# # ============================================================

# class SalesResponse(BaseModel):
#     """
#     Returned on every turn.
#     Simple: just the AI message. Frontend handles all UI state.
#     """
#     message: str                              # AI salesman reply
#     suggested_ids: Optional[List[str]] = []  # sub_service IDs AI recommends (so frontend can highlight them)
#     audio_url: Optional[str] = None          # presigned S3 URL for TTS mp3 — only present on voice endpoint

"""
salesAssistant_schema.py
------------------------
Schema for the 6-screen sales assistant flow.

Frontend owns all data and UI state.
Backend owns conversation history (Redis) and AI responses.

Flow:
  Screen 1 — Personal Info          → first name triggers AI greeting
  Screen 2 — Service Selection      → user picks Phone / Internet / TV
  Screen 3 — Package Options        → standard or customized packages
  Screen 4 — Discount & Eligibility → eligibility or employer discount lookup
  Screen 5 — Additional Instructions → delivery notes
  Screen 6 — Confirmation           → closing statement, session wiped
"""

from pydantic import BaseModel
from typing import Optional, List, Dict
from enum import Enum


# ============================================================
#  Screen identifier
# ============================================================

class Screen(str, Enum):
    personal_info           = "personal_info"
    service_selection       = "service_selection"
    package_options         = "package_options"
    discount                = "discount"
    additional_instructions = "additional_instructions"
    confirmation            = "confirmation"


# ============================================================
#  Per-screen data models
# ============================================================

class PersonalInfoData(BaseModel):
    """Screen 1 — sent when first name is entered (AI trigger)."""
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    home_address: Optional[str] = None


class ServiceSelectionData(BaseModel):
    """Screen 2 — which top-level services the user selected."""
    selected_services: Optional[List[str]] = []


class PackageOption(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    price: Optional[float] = None
    price_label: Optional[str] = None
    features: Optional[List[str]] = []
    tag: Optional[str] = None


class PackageOptionsData(BaseModel):
    """Screen 3 — standard + optional custom packages."""
    standard_packages: Optional[List[PackageOption]] = []
    custom_packages: Optional[List[PackageOption]] = []
    selected_package: Optional[PackageOption] = None


class EmployerDiscount(BaseModel):
    code: str
    description: str


class DiscountData(BaseModel):
    """Screen 4 — eligibility selection and employer discount map."""
    eligibility_options: Optional[List[str]] = []
    eligibility_selected: Optional[str] = None
    employer_discounts: Optional[Dict[str, EmployerDiscount]] = {}
    applied_promo_code: Optional[str] = None


class AdditionalInstructionsData(BaseModel):
    """Screen 5 — delivery notes."""
    instructions: Optional[str] = None


class ConfirmationData(BaseModel):
    """Screen 6 — summary for AI closing context."""
    selected_services: Optional[List[str]] = []
    selected_package: Optional[str] = None
    eligibility: Optional[str] = None
    promo_code: Optional[str] = None
    additional_instructions: Optional[str] = None


# ============================================================
#  Unified request model
# ============================================================

class SalesRequest(BaseModel):
    """
    Single request model for all 6 screens, both text and voice endpoints.

    session_id:  UUID from frontend — reused every turn.
    screen:      Current screen — determines AI prompt and context.
    message:     User text or Whisper transcript. Empty string for
                 screen-load triggers (AI speaks first).
    """
    session_id: str
    screen: Screen
    message: str = ""

    personal_info: Optional[PersonalInfoData] = None
    service_selection: Optional[ServiceSelectionData] = None
    package_options: Optional[PackageOptionsData] = None
    discount: Optional[DiscountData] = None
    additional_instructions: Optional[AdditionalInstructionsData] = None
    confirmation: Optional[ConfirmationData] = None


# ============================================================
#  Response model
# ============================================================

class SalesResponse(BaseModel):
    message: str
    audio_url: Optional[str] = None
    promo_code: Optional[str] = None           # set when employer discount matched
    suggested_package_ids: Optional[List[str]] = []