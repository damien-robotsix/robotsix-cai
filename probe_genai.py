import inspect
import genai_prices

for name, obj in inspect.getmembers(genai_prices):
    if inspect.ismodule(obj) or inspect.isfunction(obj) or inspect.isclass(obj):
        print(f"{name}: {obj}")
