try:
    import genai_prices
    import inspect
    print(dir(genai_prices))
    for mod in ['prices', 'get_prices']:
        try:
            m = getattr(genai_prices, mod, None)
            if m:
                print(f"genai_prices.{mod}:", dir(m))
        except Exception as e:
            print(e)
except Exception as e:
    print(e)
