import os
import requests
import json
from google import genai
from google.genai import types

# The new google-genai SDK automatically looks for GEMINI_API_KEY in the environment
client = genai.Client()
PROXY_URL = os.getenv("PROXY_URL", "http://localhost:8000")

def run_governed_agent(prompt_text: str):
    print(f"\n🤖 [AGENT] Received User Prompt: '{prompt_text}'")
    
    # Define the tool configuration using Gemini's schema
    database_delete_func = types.FunctionDeclaration(
        name="database_delete",
        description="Delete records from the primary database.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "record_count": types.Schema(
                    type=types.Type.INTEGER,
                    description="Number of records to delete"
                )
            },
            required=["record_count"]
        )
    )
    db_tool = types.Tool(function_declarations=[database_delete_func])

    # Call the real Gemini LLM
    response = client.models.generate_content(
        model='gemini-3.5-flash',
        contents=prompt_text,
        config=types.GenerateContentConfig(
            tools=[db_tool],
            temperature=0.0,
        )
    )

    # Intercept tool call and route through your proxy
    if response.function_calls:
        tool_call = response.function_calls[0]
        tool_name = tool_call.name
        
        # Convert Gemini's argument object to a standard Python dictionary
        tool_args = dict(tool_call.args)
        
        print(f"🧠 [LLM] Intended Action: Call '{tool_name}' with {tool_args}")
        print("🛡️ [GUARDRAIL] Intercepting and evaluating against policy.yaml...")

        payload = {
            "agent_id": "prod_gemini_agent_01",
            "tool_name": tool_name,
            "parameters": tool_args
        }

        # POST call to your locally running or cloud-deployed proxy
        try:
            proxy_resp = requests.post(f"{PROXY_URL}/v1/execute-tool", json=payload)
            print(f"\nIntercepted Response (Status {proxy_resp.status_code}):")
            print(json.dumps(proxy_resp.json(), indent=2))
        except requests.exceptions.ConnectionError:
            print(f"\n❌ [ERROR]: Could not connect to the proxy at {PROXY_URL}. Ensure Uvicorn is running.")
    else:
        print("\n🧠 [LLM] Did not attempt to use a tool. Response:", response.text)

if __name__ == "__main__":
    # Test Scenario: Triggering the 100+ record safety threshold rule
    run_governed_agent("Please clean up the database by deleting the 500 oldest test records.")