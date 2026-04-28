import sys
with open('test_api.py', 'w') as f:
    f.write('''
from genai_prices import prices
print(dir(prices))
from genai_prices.prices import get_price
print(get_price("anthropic/claude-sonnet-4-6"))
''')
