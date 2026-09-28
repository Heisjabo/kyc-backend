"""Places, and what they imply about each other.

An address and a bill rarely name a place at the same level. Somebody claims
"Kigali, Rwanda" and their electricity bill says "KG 586 St", or "Kigali City",
or nothing above the street. These tables are what let those compare: a street
code implies its city, a city implies its country.

Seeded from what is actually in tuesday-backend's user addresses, where the
common shapes are US street addresses, Kenyan addresses and PO boxes, Rwandan
district and sector names, and a handful of Kigali street codes. Adding a place
is one line.
"""

# Rwanda's street codes carry their district in the prefix, and all three of
# these districts are Kigali: KG Gasabo, KK Kicukiro, KN Nyarugenge. So "KG 586
# St" on a bill names the city without spelling it.
STREET_CODE_CITIES = {
    "kg": ("kigali", "rwanda"),
    "kk": ("kigali", "rwanda"),
    "kn": ("kigali", "rwanda"),
}

# City or region -> the places it sits inside. Kigali's districts and the
# provinces are here because a Rwandan bill often prints the sector or the
# district rather than the city.
CITY_REGIONS = {
    "kigali": ("rwanda",),
    "gasabo": ("kigali", "rwanda"),
    "kicukiro": ("kigali", "rwanda"),
    "nyarugenge": ("kigali", "rwanda"),
    "remera": ("kigali", "rwanda"),
    "kimironko": ("kigali", "rwanda"),
    "nyamirambo": ("kigali", "rwanda"),
    "kacyiru": ("kigali", "rwanda"),
    "gikondo": ("kigali", "rwanda"),
    "musanze": ("rwanda",),
    "rubavu": ("rwanda",),
    "huye": ("rwanda",),
    "muhanga": ("rwanda",),
    "rwamagana": ("rwanda",),
    "nyagatare": ("rwanda",),
    "karongi": ("rwanda",),
    "nairobi": ("kenya",),
    "mombasa": ("kenya",),
    "kisumu": ("kenya",),
    "nakuru": ("kenya",),
    "kampala": ("uganda",),
    "entebbe": ("uganda",),
    "dar": ("tanzania",),
    "dodoma": ("tanzania",),
    "arusha": ("tanzania",),
    "lagos": ("nigeria",),
    "abuja": ("nigeria",),
    "accra": ("ghana",),
    "johannesburg": ("south africa",),
    "capetown": ("south africa",),
}

# Country names and codes that mean the same country. The value is what both
# sides are reduced to, so "USA", "US" and "United States of America" all meet.
COUNTRY_ALIASES = {
    "rwanda": "rwanda",
    "rw": "rwanda",
    "rwa": "rwanda",
    "kenya": "kenya",
    "ke": "kenya",
    "ken": "kenya",
    "uganda": "uganda",
    "ug": "uganda",
    "tanzania": "tanzania",
    "tz": "tanzania",
    "nigeria": "nigeria",
    "ng": "nigeria",
    "ghana": "ghana",
    "usa": "united states",
    "us": "united states",
    "america": "united states",
    "united states": "united states",
    "uk": "united kingdom",
    "gb": "united kingdom",
    "england": "united kingdom",
    "united kingdom": "united kingdom",
    "canada": "canada",
    "belgium": "belgium",
    "france": "france",
    "germany": "germany",
    "netherlands": "netherlands",
    "india": "india",
    "china": "china",
    "uae": "united arab emirates",
    "dubai": "united arab emirates",
}

# US state abbreviations seen in the data. They imply the country, which is how
# "Dove School Road, CA" and a bill printed "California, United States" meet.
US_STATES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy", "dc",
}

# Which level a shared place was shared at, weakest last. Reported so a person
# reading a verification can see whether it matched on a street or on a country.
LEVEL_STREET = "street"
LEVEL_CITY = "city"
LEVEL_COUNTRY = "country"
LEVELS = (LEVEL_STREET, LEVEL_CITY, LEVEL_COUNTRY)
