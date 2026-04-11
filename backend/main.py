"""
TableUs — FastAPI Backend

The app follows the original hackathon flow:
  1. Food photo → Gemini Vision analysis (dish, cuisine, description)
  2. NL review → Gemini taste profile update (bullet-point style)
  3. NL search → cuisine detection (Gemini Lite) → quality-sorted candidates → Gemini ranking
  4. Group search → merge preferences (Gemini) → group-aware Gemini ranking

Single external dependency: Google Gemini.
"""
import os
import io
import json
import math
import re
import time
from pathlib import Path
from typing import List, Optional
from contextlib import asynccontextmanager
from difflib import get_close_matches

from dotenv import load_dotenv

# Always load backend/.env (not cwd-dependent). override=True so this file wins over
# empty/wrong GEMINI_API_KEY accidentally exported in the shell or IDE.
_BACKEND_ROOT = Path(__file__).resolve().parent
load_dotenv(_BACKEND_ROOT / ".env", override=True)

import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted, NotFound, InvalidArgument
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import data
from google_maps_service import GoogleMapsServiceError, get_google_maps_service

# ---------------------------------------------------------------------------
# Gemini setup
# Default to Gemini 2.5 Flash-Lite for faster, lower-latency responses and keep
# Gemini 2.5 Flash as the higher-capability fallback.
# ---------------------------------------------------------------------------
_models: dict = {}   # keyed by model name

ALLOWED_CUISINES = {
    "American", "Italian", "French", "Chinese", "Japanese", "Mexican",
    "Indian", "Thai", "Greek", "Spanish", "Korean", "Vietnamese",
    "Lebanese", "Turkish", "Moroccan", "Ethiopian", "Brazilian",
    "Peruvian", "Cuban", "German", "Portuguese", "Filipino",
    "Malaysian", "Indonesian", "Mediterranean", "Seafood",
}

# Model cascade — if the primary quota is exhausted, fall back in order.
# Each model has its own separate per-model quota.
_PRIMARY   = "gemini-2.5-flash-lite"
_FALLBACKS = ["gemini-2.5-flash", "gemini-2.0-flash-lite", "gemini-2.0-flash"]


def _get_gemini_api_key() -> str:
    """Read key from env after load_dotenv; strip whitespace and common stray quotes."""
    raw = os.getenv("GEMINI_API_KEY") or ""
    return raw.strip().strip('\'"')


def _build_model(name: str):
    api_key = _get_gemini_api_key()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")
    genai.configure(api_key=api_key)
    return genai.GenerativeModel(name)


def _call_with_fallback(prompt, image=None) -> str:
    """
    Call Gemini with automatic model fallback on ResourceExhausted (429).
    Tries _PRIMARY first, then each model in _FALLBACKS.
    """
    models_to_try = [_PRIMARY] + _FALLBACKS
    last_err = None
    for model_name in models_to_try:
        if model_name not in _models:
            _models[model_name] = _build_model(model_name)
        try:
            m = _models[model_name]
            if image is not None:
                resp = m.generate_content([prompt, image])
            else:
                resp = m.generate_content(prompt)
            return resp.text
        except InvalidArgument as e:
            err = str(e).lower()
            if "api key" in err or "api_key_invalid" in err:
                raise HTTPException(
                    status_code=401,
                    detail=(
                        "Gemini rejected GEMINI_API_KEY (invalid, revoked, or wrong product). "
                        "Create a Generative Language key at https://aistudio.google.com/apikey , "
                        "put it in backend/.env as GEMINI_API_KEY=your_key (no quotes), and restart uvicorn. "
                        "If the key is correct, check AI Studio restrictions (HTTP referrer blocks server use)."
                    ),
                ) from e
            raise
        except (ResourceExhausted, NotFound) as e:
            print(f"[FALLBACK] {model_name} unavailable ({type(e).__name__}), trying next…")
            last_err = e
    # all models exhausted
    raise HTTPException(
        status_code=429,
        detail=(
            "Gemini API quota exhausted for all available models. "
            "Please wait a minute and try again, or enable billing at "
            "https://console.cloud.google.com/billing to increase limits."
        ),
    )


# Keep thin wrappers so existing call sites don't change
def get_model():
    if _PRIMARY not in _models:
        _models[_PRIMARY] = _build_model(_PRIMARY)
    return _models[_PRIMARY]

def get_model_lite():
    lite = "gemini-2.5-flash-lite"
    if lite not in _models:
        _models[lite] = _build_model(lite)
    return _models[lite]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def clean_json_response(text: str) -> str:
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def parse_gemini_json(text: str) -> dict:
    return json.loads(clean_json_response(text))


def validate_cuisine(raw: str) -> str:
    for c in ALLOWED_CUISINES:
        if raw.lower() == c.lower():
            return c
    return raw


def get_maps_service():
    try:
        return get_google_maps_service()
    except GoogleMapsServiceError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


def compute_quality_score(restaurant: dict) -> float:
    rating = float(restaurant.get("rating", 0) or 0)
    review_count = int(restaurant.get("user_ratings_total", 0) or 0)
    return rating * math.log(review_count + 1)


def dedupe_restaurants(restaurants: list) -> list:
    seen: set[str] = set()
    deduped = []
    for restaurant in restaurants:
        key = restaurant.get("place_id") or restaurant.get("id") or restaurant.get("name")
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(restaurant)
    return deduped


def sort_by_quality(restaurants: list) -> list:
    for restaurant in restaurants:
        restaurant["quality_score"] = compute_quality_score(restaurant)
    restaurants.sort(
        key=lambda restaurant: (
            restaurant.get("quality_score", 0.0),
            -(restaurant.get("distance_meters", float("inf"))),
        ),
        reverse=True,
    )
    return restaurants


def fetch_nearby_candidates(
    latitude: float,
    longitude: float,
    radius_meters: int,
    limit: int = 20,
    keyword: Optional[str] = None,
) -> list:
    maps_service = get_maps_service()
    restaurants = maps_service.search_nearby_restaurants(
        latitude=latitude,
        longitude=longitude,
        radius_meters=radius_meters,
        limit=limit,
        keyword=keyword,
    )
    return sort_by_quality(restaurants)


def filter_restaurants_by_cuisine(restaurants: list, cuisine: Optional[str]) -> list:
    if not cuisine:
        return restaurants
    filtered = [
        restaurant
        for restaurant in restaurants
        if cuisine.lower() in (restaurant.get("cuisine") or "").lower()
    ]
    return filtered


def parse_preferences_locally(preferences_text: str) -> dict:
    text = preferences_text.lower()
    cuisines = [
        cuisine
        for cuisine in sorted(ALLOWED_CUISINES)
        if cuisine.lower() in text
    ]

    atmosphere_keywords = [
        "casual", "cozy", "upscale", "romantic", "lively", "vibrant", "warm",
        "communal", "intimate", "elegant", "trendy", "retro", "energetic",
        "family-friendly", "classic", "garden", "waterfront", "bustling",
        "cultural",
    ]
    atmospheres = [word for word in atmosphere_keywords if word in text]

    price_hints = []
    for amount_range in re.findall(r"\$\d+(?:-\$\d+)?(?:\s*per person)?", preferences_text):
        cleaned = amount_range.replace(" per person", "")
        if cleaned not in price_hints:
            price_hints.append(cleaned)
    if not price_hints:
        for price in ["$$$$", "$$$", "$$", "$"]:
            if price in preferences_text and price not in price_hints:
                price_hints.append(price)

    flavor_keywords = [
        "spicy", "savory", "umami", "rich", "smoky", "tangy", "aromatic",
        "delicate", "bold", "fermented", "fresh", "sweet", "creamy",
    ]
    flavor_tags = [word for word in flavor_keywords if word in text]

    return {
        "cuisines": cuisines,
        "atmospheres": atmospheres,
        "price_hints": price_hints,
        "flavor_tags": flavor_tags,
    }


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        get_model()
        print("[STARTUP] Gemini model ready")
    except Exception as e:
        print(f"[STARTUP] Gemini not configured yet: {e}")
    yield


app = FastAPI(title="TableUs", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ===================================================================
# 1. FOOD PHOTO ANALYSIS — mirrors services/gemini_service.py
# ===================================================================

@app.post("/api/food/analyze")
async def analyze_food_image(image: UploadFile = File(...)):
    """
    Upload a food photo → Gemini Vision identifies dish, cuisine, description, flavor tags.
    Mirrors original analyze_food_image() with cuisine whitelist validation.
    """
    from PIL import Image

    image_bytes = await image.read()
    pil_image = Image.open(io.BytesIO(image_bytes))

    prompt = """Identify this food image and return ONLY valid JSON (no markdown):
{
  "dish": "Name of the dish (e.g. Spaghetti Carbonara, California Roll)",
  "cuisine": "ONE cuisine type from: American, Italian, French, Chinese, Japanese, Mexican, Indian, Thai, Korean, Vietnamese, Greek, Spanish, Ethiopian, Mediterranean, Seafood, Brazilian, Lebanese, Turkish",
  "description": "One sentence about the food — its flavors, textures, and key ingredients",
  "flavor_tags": ["savory", "umami", "rich"]
}"""

    raw = _call_with_fallback(prompt, image=pil_image)
    result = parse_gemini_json(raw)
    result["cuisine"] = validate_cuisine(result.get("cuisine", "Unknown"))
    return result


# ===================================================================
# 2. REVIEWS + TASTE PROFILE UPDATE
#    Mirrors original taste_profile_service.py update pattern
# ===================================================================

class ReviewSubmitRequest(BaseModel):
    user_id: str
    restaurant_name: str
    review_text: str
    rating: float
    dish: Optional[str] = None
    cuisine: Optional[str] = None


@app.post("/api/reviews/submit")
async def submit_review(req: ReviewSubmitRequest):
    """
    Submit a NL review → save it → update taste profile via Gemini.
    Mirrors the original _build_implicit_signals_prompt style: bullet points,
    third person, strong verbs (Loves, Enjoys, Prefers...).
    """
    review = {
        "restaurant_name": req.restaurant_name,
        "review_text": req.review_text,
        "rating": req.rating,
        "dish": req.dish,
        "cuisine": req.cuisine,
    }
    saved = data.add_review(req.user_id, review)

    current_prefs = data.get_user_preferences(req.user_id)
    all_reviews = data.get_reviews(req.user_id)

    reviews_summary = "\n".join([
        f"- {r['restaurant_name']} ({r.get('cuisine', 'Unknown')}): "
        f"\"{r['review_text']}\" — {r['rating']}/5"
        for r in all_reviews[-10:]
    ])

    prompt = f"""You are a food preference analyst. Generate a natural language preference profile based on this user's reviews.

CURRENT PREFERENCES (if any):
{current_prefs if current_prefs else "(No preferences yet — create from scratch)"}

ALL REVIEWS:
{reviews_summary}

Generate a concise, scannable preference profile using bullet points and short phrases.
Write in third person (e.g., "Loves...", "Frequently visits...", "Prefers...").

GUIDELINES:
1. Use bullet points with "•"
2. Be specific — mention actual restaurants, favorite dishes, cuisines
3. Group by theme: cuisines, dining style, atmosphere, flavors
4. 4-8 bullet points total
5. Start bullets with strong verbs: Loves, Enjoys, Prefers, Drawn to, Appreciates
6. If current preferences exist, MERGE new insights rather than replacing

EXAMPLE:
• Loves Japanese and Thai cuisine — especially fresh sushi and spicy curries
• Prefers casual, vibrant atmospheres for everyday dining
• Appreciates upscale spots for special occasions
• Drawn to bold, umami-rich, and aromatic flavors
• Usually lands around $15-$35 per person
• Enjoys communal dining and shareable plates

Return ONLY the preference text (no JSON, no markdown fences)."""

    new_profile = _call_with_fallback(prompt).strip()
    if new_profile.startswith('"') and new_profile.endswith('"'):
        new_profile = new_profile[1:-1]

    data.set_user_preferences(req.user_id, new_profile)

    return {
        "review": saved,
        "updated_taste_profile": new_profile,
    }


@app.get("/api/reviews/{user_id}")
async def get_reviews(user_id: str):
    return data.get_reviews(user_id)


# ===================================================================
# 3. TASTE PROFILE — mirrors taste_profile_service.py
# ===================================================================

@app.get("/api/profile/{user_id}/taste")
async def get_taste_profile(user_id: str):
    """
    Return user's taste profile text + structured parse.
    Uses a fast local parser so the profile screen is instant and does not depend
    on an extra Gemini round-trip.
    """
    prefs_text = data.get_user_preferences(user_id)
    if not prefs_text:
        return {
            "preferences_text": "",
            "structured": {
                "cuisines": [], "atmospheres": [],
                "price_hints": [], "flavor_tags": [],
            },
        }

    structured = parse_preferences_locally(prefs_text)
    return {"preferences_text": prefs_text, "structured": structured}


# ===================================================================
# 4. RESTAURANT SEARCH — mirrors restaurant_search_service.py
#    Pipeline: prefs → cuisine detection → candidates → LLM rank
# ===================================================================

class LocationResolveRequest(BaseModel):
    query: str


class NearbyRequest(BaseModel):
    latitude: float
    longitude: float
    radius_meters: int = 2000
    limit: int = 18


class SearchRequest(BaseModel):
    query: str
    user_id: str
    latitude: float
    longitude: float
    location_label: str
    radius_meters: int = 2000


class GroupSearchRequest(BaseModel):
    query: str
    user_ids: List[str]
    latitude: float
    longitude: float
    location_label: str
    radius_meters: int = 2000


def extract_cuisine_preferences(preferences_text: str) -> List[str]:
    cuisines: List[str] = []
    try:
        struct_prompt = f"""Extract ONLY the cuisine types from this text. Return JSON:
{{"cuisines": ["Italian", "Japanese"]}}

Text: {preferences_text}"""
        cuisines = parse_gemini_json(_call_with_fallback(struct_prompt)).get("cuisines", [])
    except Exception:
        pass
    return cuisines


def build_restaurants_prompt_text(restaurants: list) -> str:
    return "\n".join([
        f"{i+1}. {restaurant['name']}\n"
        f"   - Cuisine: {restaurant['cuisine']}\n"
        f"   - Rating: {restaurant['rating']}★ ({restaurant['user_ratings_total']} reviews)\n"
        f"   - Price: {'$' * restaurant['price_level']}\n"
        f"   - Atmosphere: {restaurant['atmosphere']}\n"
        f"   - Distance: {restaurant.get('distance_label', 'Distance unavailable')}\n"
        f"   - Address: {restaurant['address']}\n"
        f"   - About: {restaurant['description']}"
        for i, restaurant in enumerate(restaurants[:20])
    ])


def prepare_nearby_pool(
    latitude: float,
    longitude: float,
    radius_meters: int,
    detected_cuisine: Optional[str] = None,
    limit: int = 20,
) -> tuple[list, int]:
    nearby_pool = fetch_nearby_candidates(
        latitude=latitude,
        longitude=longitude,
        radius_meters=radius_meters,
        limit=limit,
    )
    radius_used = radius_meters

    if len(nearby_pool) < 8 and radius_meters < 5000:
        expanded_pool = fetch_nearby_candidates(
            latitude=latitude,
            longitude=longitude,
            radius_meters=5000,
            limit=limit,
        )
        nearby_pool = sort_by_quality(dedupe_restaurants(nearby_pool + expanded_pool))
        radius_used = 5000

    if detected_cuisine:
        cuisine_matches = filter_restaurants_by_cuisine(nearby_pool, detected_cuisine)
        if len(cuisine_matches) < 4:
            keyword_pool = fetch_nearby_candidates(
                latitude=latitude,
                longitude=longitude,
                radius_meters=radius_used,
                limit=limit,
                keyword=detected_cuisine,
            )
            nearby_pool = sort_by_quality(dedupe_restaurants(nearby_pool + keyword_pool))

    return nearby_pool, radius_used


@app.post("/api/location/resolve")
async def resolve_location(req: LocationResolveRequest):
    maps_service = get_maps_service()
    try:
        return maps_service.resolve_location(req.query)
    except GoogleMapsServiceError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/api/restaurants/nearby")
async def get_nearby_restaurants(req: NearbyRequest):
    nearby_pool, radius_used = prepare_nearby_pool(
        latitude=req.latitude,
        longitude=req.longitude,
        radius_meters=req.radius_meters,
        limit=req.limit,
    )
    return {
        "status": "success",
        "restaurants": nearby_pool[: req.limit],
        "radius_meters": radius_used,
        "count": len(nearby_pool[: req.limit]),
    }


@app.post("/api/restaurants/search")
async def search_restaurants(req: SearchRequest):
    """
    Natural-language restaurant search.
    Precisely mirrors RestaurantSearchService.search_restaurants():
      Step 1: Get user preferences + parse to structured
      Step 2: Detect cuisine from query (Gemini Lite)
      Step 3: Get candidates, filter by cuisine, sort by quality_score
      Step 4: LLM ranking with weighted criteria prompt
      Step 5: Fuzzy-match enrichment
    """
    t0 = time.time()

    # Step 1 — user preferences (mirrors get_user_preferences_tool)
    prefs_text = data.get_user_preferences(req.user_id)
    if not prefs_text:
        prefs_text = (
            "No specific preferences yet. Assume they like good quality food "
            "with positive vibes and high ratings."
        )

    # Parse structured cuisines for filtering (mirrors parse_preferences_to_structured)
    user_cuisines = extract_cuisine_preferences(prefs_text)

    # Step 2 — cuisine detection (mirrors _detect_cuisine_from_query)
    detected_cuisine = await _detect_cuisine(req.query)

    # Step 3 — nearby candidates
    nearby_pool, radius_used = prepare_nearby_pool(
        latitude=req.latitude,
        longitude=req.longitude,
        radius_meters=req.radius_meters,
        detected_cuisine=detected_cuisine,
        limit=20,
    )
    restaurants = list(nearby_pool)
    if not restaurants:
        return {
            "status": "success",
            "query": req.query,
            "search_summary": f"No nearby restaurants found around {req.location_label}.",
            "top_restaurants": [],
            "nearby_restaurants": [],
            "user_preferences": prefs_text,
            "location": {
                "label": req.location_label,
                "latitude": req.latitude,
                "longitude": req.longitude,
                "radius_meters": radius_used,
            },
            "elapsed_ms": int((time.time() - t0) * 1000),
        }

    if detected_cuisine:
        filtered = filter_restaurants_by_cuisine(restaurants, detected_cuisine)
        if filtered:
            restaurants = filtered
    elif user_cuisines:
        top2 = user_cuisines[:2]
        filtered = [
            r for r in restaurants
            if any(c.lower() in r["cuisine"].lower() for c in top2)
        ]
        if filtered:
            restaurants = filtered

    restaurants = sort_by_quality(restaurants)

    # Step 4 — LLM ranking (mirrors the original ranking prompt precisely)
    restaurants_text = build_restaurants_prompt_text(restaurants)

    prompt = f"""You are a restaurant recommendation expert. Analyze these restaurants and select the TOP 4 that best match the user's query.

USER'S QUERY: "{req.query}"
SEARCH LOCATION: {req.location_label}

USER'S PREFERENCES:
{prefs_text}

Preferred Cuisines: {', '.join(user_cuisines) if user_cuisines else 'Open to all cuisines'}

AVAILABLE RESTAURANTS:
{restaurants_text}

RANKING CRITERIA (in priority order):
1. **QUERY MATCH (40%)**: Does the restaurant match what the user explicitly asked for?
   - If query mentions specific cuisine, ONLY recommend that cuisine
   - If query mentions atmosphere (e.g., "romantic", "casual"), prioritize that
2. **RATING QUALITY (30%)**: Higher ratings + more reviews = more reliable
3. **USER PREFERENCES (30%)**: Match preferred cuisines, atmospheres, flavors

Return ONLY valid JSON (no markdown, no code blocks):
{{
  "restaurants": [
    {{
      "name": "Exact restaurant name from the list above",
      "match_score": 0.95,
      "reasoning": "Brief reason why this matches (1 sentence, max 20 words)"
    }}
  ],
  "search_summary": "One friendly sentence explaining what you found"
}}

Return exactly 4 restaurants. Use EXACT names from the list."""

    llm_result = parse_gemini_json(_call_with_fallback(prompt))

    # Step 5 — enrich with full restaurant data (mirrors fuzzy_match_restaurant)
    enriched = _enrich_results(
        llm_result.get("restaurants", []), restaurants
    )

    return {
        "status": "success",
        "query": req.query,
        "search_summary": llm_result.get("search_summary", ""),
        "top_restaurants": enriched[:4],
        "nearby_restaurants": nearby_pool[:16],
        "user_preferences": prefs_text,
        "location": {
            "label": req.location_label,
            "latitude": req.latitude,
            "longitude": req.longitude,
            "radius_meters": radius_used,
        },
        "elapsed_ms": int((time.time() - t0) * 1000),
    }


# ===================================================================
# 5. GROUP SEARCH — mirrors search_restaurants_for_group()
#    Extra step: merge preferences via Gemini first
# ===================================================================


@app.post("/api/restaurants/search-group")
async def search_group(req: GroupSearchRequest):
    """
    Group restaurant search with merged taste profiles.
    Mirrors RestaurantSearchService.search_restaurants_for_group() precisely:
      1. Merge preferences (Gemini)
      2. Detect cuisine (Gemini Lite)
      3. Get candidates
      4. Group-aware LLM ranking with QUERY OVERRIDE rules
      5. Enrich
    """
    t0 = time.time()

    # Step 1 — gather + merge preferences (mirrors merge_multiple_user_preferences)
    individual_prefs = []
    user_names = []
    for uid in req.user_ids:
        user = data.get_user(uid)
        if not user:
            continue
        user_names.append(user["name"])
        pref = data.get_user_preferences(uid)
        if pref:
            individual_prefs.append(f"{user['name']}: {pref}")

    if len(individual_prefs) >= 2:
        if len(user_names) == 2:
            group_phrase = f"{user_names[0]} and {user_names[1]}"
        else:
            group_phrase = f"{user_names[0]} and {len(user_names) - 1} friends"

        merge_prompt = f"""You are merging dining preferences for a group of friends.

Here are the individual preferences:

{chr(10).join([f"Person {i+1}: {p}" for i, p in enumerate(individual_prefs)])}

Task: Create a single, concise group preference profile (MAXIMUM 2 sentences) that:
1. Start with "{group_phrase}"
2. Highlight common preferences and interesting contrasts
3. Be conversational and friendly

Return ONLY the merged preference text."""

        merged_prefs = _call_with_fallback(merge_prompt).strip().strip('"')
        has_group_preferences = True
    elif individual_prefs:
        merged_prefs = individual_prefs[0]
        has_group_preferences = True
    else:
        merged_prefs = (
            f"Group of {len(req.user_ids)} diners with varied tastes "
            "looking for a versatile restaurant."
        )
        has_group_preferences = False

    # Step 2 — cuisine detection
    detected_cuisine = await _detect_cuisine(req.query)

    # Step 3 — nearby candidates
    nearby_pool, radius_used = prepare_nearby_pool(
        latitude=req.latitude,
        longitude=req.longitude,
        radius_meters=req.radius_meters,
        detected_cuisine=detected_cuisine,
        limit=20,
    )
    restaurants = list(nearby_pool)
    if not restaurants:
        return {
            "status": "success",
            "query": req.query,
            "search_summary": f"No nearby restaurants found around {req.location_label}.",
            "top_restaurants": [],
            "nearby_restaurants": [],
            "merged_preferences": merged_prefs,
            "user_count": len(req.user_ids),
            "location": {
                "label": req.location_label,
                "latitude": req.latitude,
                "longitude": req.longitude,
                "radius_meters": radius_used,
            },
            "elapsed_ms": int((time.time() - t0) * 1000),
        }

    if detected_cuisine:
        filtered = filter_restaurants_by_cuisine(restaurants, detected_cuisine)
        if filtered:
            restaurants = filtered

    restaurants = sort_by_quality(restaurants)

    # Step 4 — group-aware LLM ranking (mirrors the original group prompt)
    restaurants_text = build_restaurants_prompt_text(restaurants)

    prompt = f"""You are a restaurant recommendation expert selecting for a GROUP of {len(req.user_ids)} people dining together.

USER'S QUERY: "{req.query}"
SEARCH LOCATION: {req.location_label}

GROUP'S MERGED PREFERENCES:
{merged_prefs}

AVAILABLE RESTAURANTS:
{restaurants_text}

GROUP RANKING RULES (PRIORITY ORDER):
1. **QUERY OVERRIDE FOR CUISINE**: If the query mentions a specific cuisine/food type (e.g., "sushi night"), ONLY recommend that cuisine.
   - BUT STILL USE group preferences for atmosphere, price, and flavor!
2. **BLEND QUERY + PREFERENCES**: Query sets the cuisine/main requirement, preferences refine the selection.
3. **EMPTY GROUP PREFERENCES**: If group has minimal preferences, rely on query and prioritize highly-rated, group-friendly restaurants.
4. **VAGUE QUERIES**: If query is vague ("where should we eat?"), use merged preferences fully.

GROUP CONTEXT:
- Dining with {len(req.user_ids)} people — ensure restaurants can accommodate
- Merged preferences represent ALL members — aim to satisfy everyone
- GROUP HAS {'DIVERSE' if has_group_preferences else 'MINIMAL'} PREFERENCES

Return ONLY valid JSON (no markdown, no code blocks):
{{
  "restaurants": [
    {{
      "name": "Exact name from the list above",
      "match_score": 0.95,
      "reasoning": "Why this works for the group (1 sentence, max 20 words)"
    }}
  ],
  "search_summary": "One sentence explaining the group recommendation"
}}

Return exactly 4 restaurants."""

    llm_result = parse_gemini_json(_call_with_fallback(prompt))

    # Step 5 — enrich
    enriched = _enrich_results(
        llm_result.get("restaurants", []), restaurants
    )

    return {
        "status": "success",
        "query": req.query,
        "search_summary": llm_result.get("search_summary", ""),
        "top_restaurants": enriched[:4],
        "nearby_restaurants": nearby_pool[:16],
        "merged_preferences": merged_prefs,
        "user_count": len(req.user_ids),
        "location": {
            "label": req.location_label,
            "latitude": req.latitude,
            "longitude": req.longitude,
            "radius_meters": radius_used,
        },
        "elapsed_ms": int((time.time() - t0) * 1000),
    }


# ===================================================================
# 6. BLEND PREFERENCES — mirrors routers/preferences.py /blend
# ===================================================================

class BlendRequest(BaseModel):
    user_ids: List[str]


@app.post("/api/preferences/blend")
async def blend_preferences(req: BlendRequest):
    """
    Blend taste preferences for a group and extract structured data.
    Mirrors the original /preferences/blend endpoint.
    """
    individual_prefs = []
    user_names = []
    for uid in req.user_ids:
        user = data.get_user(uid)
        if not user:
            continue
        user_names.append(user["name"])
        pref = data.get_user_preferences(uid)
        if pref:
            individual_prefs.append(pref)

    if len(individual_prefs) >= 2:
        group_label = " and ".join(user_names[:2])
        if len(user_names) > 2:
            group_label += f" and {len(user_names) - 2} more"

        merge_prompt = f"""Merge these dining preferences for a group of friends.

{chr(10).join([f"Person {i+1} ({user_names[i]}): {p}" for i, p in enumerate(individual_prefs)])}

Create ONE concise group profile (2 sentences max). Start with "{group_label}".
Be conversational. Return ONLY the text."""

        blended_text = _call_with_fallback(merge_prompt).strip().strip('"')
    elif individual_prefs:
        blended_text = individual_prefs[0]
    else:
        blended_text = "Group with varied tastes."

    # Extract structured data (mirrors extract_structured_preferences)
    structured = {"cuisines": [], "atmosphere": [], "price_range": "$25-$45 per person"}
    try:
        extract_prompt = f"""From this group's dining preferences, extract structured information.

Merged group preference:
{blended_text}

Individual preferences:
{chr(10).join([f"- {p}" for p in individual_prefs])}

Return ONLY valid JSON:
{{
  "cuisines": ["Italian", "Japanese", "Mexican"],
  "atmosphere": ["casual", "cozy"],
  "price_range": "$25-$45 per person"
}}

Use an actual numeric dollar range for "price_range" whenever possible."""

        structured = parse_gemini_json(_call_with_fallback(extract_prompt))
    except Exception:
        pass

    return {
        "blended_text": blended_text,
        "user_count": len(req.user_ids),
        "user_names": user_names,
        "top_cuisines": structured.get("cuisines", []),
        "atmosphere_preferences": structured.get("atmosphere", []),
        "price_range": structured.get("price_range", "$25-$45 per person"),
    }


# ===================================================================
# 7. FRIENDS — mirrors routers/friends.py mutual friend graph
# ===================================================================

@app.get("/api/friends/{user_id}")
async def get_friends(user_id: str):
    """Return the user's friend list with profiles."""
    return data.get_friends(user_id)


class FriendAction(BaseModel):
    user_id: str
    friend_id: str


@app.post("/api/friends/add")
async def add_friend(req: FriendAction):
    """Mutual add — both users become friends."""
    ok = data.add_friend(req.user_id, req.friend_id)
    if not ok:
        raise HTTPException(404, "User not found")
    return {"status": "ok"}


@app.post("/api/friends/remove")
async def remove_friend(req: FriendAction):
    """Mutual remove."""
    ok = data.remove_friend(req.user_id, req.friend_id)
    if not ok:
        raise HTTPException(404, "User not found")
    return {"status": "ok"}


# ===================================================================
# 8. USERS
# ===================================================================

@app.get("/api/users")
async def list_users():
    return data.get_all_users()


@app.get("/api/users/{user_id}")
async def get_user(user_id: str):
    user = data.get_user(user_id)
    if not user:
        raise HTTPException(404, "User not found")
    return {"id": user["id"], "name": user["name"], "avatar": user["avatar"]}


# ===================================================================
# 8. HEALTH
# ===================================================================

@app.get("/")
@app.get("/health")
async def health():
    has_key = bool(_get_gemini_api_key())
    has_maps_key = bool((os.getenv("GOOGLE_MAPS_API_KEY") or "").strip().strip("'\""))
    return {
        "status": "ok",
        "gemini_configured": has_key,
        "google_maps_configured": has_maps_key,
        "restaurants": len(data.RESTAURANTS),
        "users": len(data.DEMO_USERS),
    }


# ===================================================================
# INTERNAL: cuisine detection — mirrors _detect_cuisine_from_query
# ===================================================================

async def _detect_cuisine(query: str) -> Optional[str]:
    """Use Gemini Lite to detect cuisine type from natural language query."""
    prompt = f"""Analyze this restaurant search query and identify the cuisine type if present.

Query: "{query}"

If the query mentions a specific cuisine OR a dish strongly associated with one cuisine, return that cuisine.
Otherwise, return null.

Examples:
- "burritos" → mexican
- "find me good sushi" → japanese
- "italian near me" → italian
- "pad thai" → thai
- "best restaurants" → null
- "something upscale" → null

Return ONLY valid JSON (no markdown):
{{"cuisine": "mexican"}}  or  {{"cuisine": null}}

Supported cuisines: mexican, italian, japanese, chinese, thai, indian, french, korean, vietnamese, greek, american, seafood, mediterranean, ethiopian, portuguese"""

    try:
        result = parse_gemini_json(_call_with_fallback(prompt))
        return result.get("cuisine")
    except Exception:
        return None


# ===================================================================
# INTERNAL: enrich LLM results — mirrors fuzzy_match_restaurant
# ===================================================================

def _enrich_results(
    llm_recs: list, all_restaurants: list, min_results: int = 3
) -> list:
    """Match LLM-recommended names to full restaurant data via fuzzy matching."""
    enriched = []
    all_names = [r["name"] for r in all_restaurants]

    for rec in llm_recs:
        name = rec.get("name", "")
        match = next((r for r in all_restaurants if r["name"] == name), None)

        if not match:
            close = get_close_matches(name, all_names, n=1, cutoff=0.5)
            if close:
                match = next(
                    r for r in all_restaurants if r["name"] == close[0]
                )

        if match:
            enriched.append({
                **match,
                "match_score": rec.get("match_score", 0.8),
                "reasoning": rec.get("reasoning", ""),
            })

    if len(enriched) < min_results:
        for r in all_restaurants:
            if r["name"] not in [e["name"] for e in enriched]:
                enriched.append({
                    **r,
                    "match_score": 0.5,
                    "reasoning": "Top-rated option",
                })
            if len(enriched) >= 4:
                break

    return enriched
