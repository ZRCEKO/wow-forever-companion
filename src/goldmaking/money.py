from decimal import Decimal

MAX_COPPER = 2**63 - 1


def gold_to_copper(gold: str | Decimal) -> int:
    """Conversion exacte, sans arrondi silencieux ni flottant binaire."""
    if not isinstance(gold, (str, Decimal)):
        raise TypeError("Utiliser une chaîne ou Decimal")
    value = Decimal(gold) * 10000
    if not value.is_finite() or value != value.to_integral_value() or not 0 <= value <= MAX_COPPER:
        raise ValueError("Montant invalide ou inférieur à la précision du cuivre")
    return int(value)


def copper_to_gold(copper: int) -> str:
    if type(copper) is not int or not 0 <= copper <= MAX_COPPER:
        raise ValueError("Cuivre entier positif ou nul requis")
    return f"{Decimal(copper) / 10000:.4f}"
