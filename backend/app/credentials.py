import random
import secrets
import string

ADJECTIVES = [
    "Cool", "Ivan", "Swift", "Marta", "Irina", "Olga", "Garik",
    "Cube", "Cat", "Kevin", "Bright", "Shadow", "Neon", "Epic",
]
NOUNS = [
    "Runner", "Fox", "Coder", "Ghost", "Falcon", "Wizard", "Rider",
    "Hunter", "Pilot", "Ninja", "Tiger", "Raven", "Rocket", "Nomad",
]
ALPHABET = string.ascii_letters + string.digits + "_"


def make_username() -> str:
    adj = random.choice(ADJECTIVES)
    noun = random.choice(NOUNS)
    num = random.randint(1980, 2000)
    return f"{adj}{noun}{num}"


def make_password(password_length: int = 10) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(password_length))
