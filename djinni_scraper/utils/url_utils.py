from urllib.parse import urlencode

BASE_URL = "https://djinni.co/jobs/"


def get_start_url(params):
    """
    Генерує стартовий URL з параметрами.
    params - словник параметрів, де значення можуть бути як рядком, так і списком.
    """
    # Якщо значення - не список, перетворюємо в список (для `doseq=True`)
    params = {key: value if isinstance(value, list) else [value] for key, value in params.items()}
    query_string = urlencode(params, doseq=True)
    return f"{BASE_URL}?{query_string}"
