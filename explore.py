import sys
for p in sys.path:
    import os
    target = os.path.join(p, "genai_prices")
    if os.path.exists(target):
        import subprocess
        subprocess.run(["ls", "-l", target])
        try:
            print("--- prices.py ---")
            subprocess.run(["cat", os.path.join(target, "prices.py")])
        except Exception as e:
            print("err:", e)
        try:
            print("--- get_prices.py ---")
            subprocess.run(["cat", os.path.join(target, "get_prices.py")])
        except Exception as e:
            pass
        try:
            print("--- token_prices.py ---")
            subprocess.run(["cat", os.path.join(target, "token_prices.py")])
        except Exception as e:
            pass
try:
    from genai_prices import prices, get_prices
    print("IMPORTED")
except Exception as e:
    print(e)
