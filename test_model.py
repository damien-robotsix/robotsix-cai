from pydantic_ai.models.openai import OpenAIModel
model = OpenAIModel('anthropic/claude-sonnet-4-6')
print(dir(model))
print(model.model_name)
