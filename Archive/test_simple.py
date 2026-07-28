from google import genai

client = genai.Client()
r = client.models.generate_content(
    model="gemini-3-flash-preview",
    contents="سلام، یک جمله کوتاه بگو",
)
print(r.text)
