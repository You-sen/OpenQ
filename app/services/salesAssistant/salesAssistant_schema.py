"""
salesAssistant_schema.py
------------------------
Pydantic models for the sales assistant.

The frontend controls the entire product catalog — backend just receives
whatever JSON the frontend sends and passes it to the AI.

Two modes:
  1. CATALOG mode   — user hasn't selected anything yet (timer expired)
                      frontend sends full catalog + all discounts/promos
  2. SELECTION mode — user selected one or more items
                      frontend sends only the selected items + applicable offers
"""

from pydantic import BaseModel
from typing import Optional, List, Any


# ============================================================
#  Product / Service models
#  (mirrors whatever structure frontend sends — kept flexible)
# ============================================================

class SubService(BaseModel):
    """
    A specific plan/tier under a service category.
    Example: "Ultimate Pro Internet — $79.99/mo — Up to 1Gbps"
    """
    id: str                              # e.g. "internet_ultimate_pro"
    name: str                            # e.g. "Ultimate Pro"
    description: Optional[str] = None   # e.g. "Up to 1Gbps, unlimited data"
    price: Optional[float] = None       # monthly price
    price_label: Optional[str] = None   # e.g. "$79.99/mo" or "from $49"
    features: Optional[List[str]] = []  # bullet points frontend wants AI to reference
    tag: Optional[str] = None           # e.g. "Most Popular", "Best Value"


class Discount(BaseModel):
    """
    A discount or promo that may apply.
    Frontend decides eligibility — just sends relevant ones.
    """
    id: str                              # e.g. "promo_3month"
    label: str                           # e.g. "3-Month Bundle Discount"
    description: Optional[str] = None   # e.g. "Save 15% when you prepay 3 months"
    condition: Optional[str] = None     # e.g. "Age 35+", "First-time customer"
    value: Optional[str] = None         # e.g. "15% off" or "$10/mo off"


class Service(BaseModel):
    """
    A top-level service category with its available sub-services.
    Example: Internet → [Home Connection, Ultimate Pro, ...]
    """
    id: str                              # e.g. "internet"
    name: str                            # e.g. "Internet"
    icon: Optional[str] = None          # optional, ignored by AI
    sub_services: List[SubService] = []
    applicable_discounts: List[Discount] = []


# ============================================================
#  Request models
# ============================================================

class SalesMessage(BaseModel):
    """
    Sent on every chat turn.

    mode:
      "catalog"   — user hasn't selected anything; AI should explore and suggest
      "selection" — user selected specific items; AI should affirm and upsell

    history: last N messages for short-term context (frontend manages this)
    """
    message: str                              # the user's typed message
    mode: str = "catalog"                     # "catalog" | "selection"

    # Catalog mode: full list of all available services
    catalog: Optional[List[Service]] = None

    # Selection mode: only what the user picked
    selected_services: Optional[List[SubService]] = []
    selected_discounts: Optional[List[Discount]] = []

    # Short conversation history — frontend sends last 6 turns max
    history: Optional[List[dict]] = []        # [{"role": "user"|"assistant", "content": "..."}]

    # Optional: any extra context frontend wants to pass (e.g. user age for discount hints)
    context: Optional[dict] = None            # e.g. {"user_age": 38, "is_new_customer": True}


# ============================================================
#  Response model
# ============================================================

class SalesResponse(BaseModel):
    """
    Returned on every turn.
    Simple: just the AI message. Frontend handles all UI state.
    """
    message: str                              # AI salesman reply
    suggested_ids: Optional[List[str]] = []  # sub_service IDs AI recommends (so frontend can highlight them)
    audio_url: Optional[str] = None          # presigned S3 URL for TTS mp3 — only present on voice endpoint