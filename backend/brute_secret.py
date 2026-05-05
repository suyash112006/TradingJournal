import hashlib

string_to_sign = "folder=trading_journal/trades&public_id=2026-05-05_09-21_Tuesday_trade_screenshot_3473e5eb&timestamp=1777953066&transformation=c_limit,w_1200/q_auto/f_auto"
expected_signature = "f7a0b16a7c0b6514813d3b97ecaa6c48e02dfcde"

def check(secret):
    signature = hashlib.sha1((string_to_sign + secret).encode('utf-8')).hexdigest()
    return signature == expected_signature

base_secret = "GQlirXnKkZysKnWqhTnTCZZ69Kg"

# Try variations
variations = [
    base_secret,
    base_secret.replace('l', 'I'),
    base_secret.replace('l', '1'),
    base_secret.replace('i', 'l'),
    base_secret.replace('i', 'j'),
    base_secret.replace('n', 'm'),
    base_secret.replace('q', 'g'),
    base_secret.replace('C', 'G'),
    base_secret.replace('6', 'G'),
    base_secret.replace('9', 'g'),
    base_secret.lower(),
    base_secret.upper(),
]

# Try swapping each character with its lookalike
lookalikes = {
    'l': ['I', '1'],
    'i': ['l', 'j', '1'],
    'n': ['m'],
    'q': ['g', 'a'],
    'C': ['G', '0'],
    '6': ['b', 'G'],
    '9': ['g', 'q'],
    'h': ['b'],
}

found = False
for i in range(len(base_secret)):
    char = base_secret[i]
    if char in lookalikes:
        for replacement in lookalikes[char]:
            new_secret = base_secret[:i] + replacement + base_secret[i+1:]
            if check(new_secret):
                print(f"FOUND! Secret is: {new_secret}")
                found = True
                break
    if found: break

if not found:
    print("Not found in simple lookalikes.")
    # Try the base secret again just to be sure
    print(f"Base secret signature: {hashlib.sha1((string_to_sign + base_secret).encode('utf-8')).hexdigest()}")
    print(f"Expected:            {expected_signature}")
