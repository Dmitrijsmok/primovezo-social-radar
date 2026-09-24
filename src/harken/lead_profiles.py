"""Built-in keyword profiles for focused commercial lead monitoring."""

from __future__ import annotations

PRIMOVEZO_LEAD_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("ecommerce", "meklēju interneta veikalu"),
    ("ecommerce", "vajag interneta veikalu"),
    ("ecommerce", "interneta veikala izstrāde"),
    ("ecommerce", "interneta veikala izveide"),
    ("ecommerce", "e-veikala izstrāde"),
    ("ecommerce", "e-veikala izveide"),
    ("ecommerce", "meklēju e-komercijas platformu"),
    ("ecommerce", "e-komercijas platforma"),
    ("ecommerce", "e-komercijas risinājums"),
    ("ecommerce", "Shopify alternatīva"),
    ("ecommerce", "WooCommerce alternatīva"),
    ("ecommerce", "migrācija no Shopify"),
    ("ecommerce", "migrācija no WooCommerce"),
)


# Broader Latvian discovery queries for finding useful ecommerce conversations,
# not just posts that already contain explicit buying-intent wording.
PRIMOVEZO_DISCOVERY_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("discovery", "Shopify"),
    ("discovery", "WooCommerce"),
    ("discovery", "Mozello"),
    ("discovery", "Etsy"),
    ("discovery", "interneta veikala platforma"),
    ("discovery", "e-veikala platforma"),
    ("discovery", "veikala platforma"),
    ("discovery", "interneta veikals"),
    ("discovery", "internetveikals"),
    ("discovery", "e-komercija"),
    ("discovery", "e-veikals"),
    ("discovery", "pārdot internetā"),
    ("discovery", "veikals internetā"),
    ("discovery", "online veikals"),
)
