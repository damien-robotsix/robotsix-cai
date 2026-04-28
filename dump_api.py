import sys
import os

try:
    import genai_prices
    print("genai_prices:", dir(genai_prices))
    try:
        from genai_prices import prices
        print("genai_prices.prices:", dir(prices))
        print("genai_prices.prices.get_price:", prices.get_price)
    except Exception as e:
        print("Error getting prices:", e)
except Exception as e:
    print("Error:", e)
