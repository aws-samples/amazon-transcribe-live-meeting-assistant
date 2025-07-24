#!/bin/bash

echo "🚀 Testing Search Agent with DuckDuckGo MCP Server"
echo "================================================"

# Load environment configuration
source config.env

echo "✅ Environment configured"
echo "🔗 Using MCP Server: $DUCKDUCKGO_MCP_ARN"

# Run the search agent
python search_agent.py
