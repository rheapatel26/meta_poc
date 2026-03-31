# ai_agent.py — MCP Client + Groq Function Calling Agent
# Connects to mcp_server.py via stdio, discovers tools dynamically,
# and uses Groq's function calling to let the LLM decide which tools to invoke.

import os
import sys
import json
import asyncio
import logging
from datetime import datetime, timedelta

# Patch asyncio to allow nested event loops (required for Streamlit compatibility)
import nest_asyncio
nest_asyncio.apply()

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# ─── Existing imports (kept for backward compat + Real-Time Monitor) ───
from adb_utils import (
    adb, get_battery, get_memory, get_cpu, get_thermals,
    get_foreground_pkg, collect_snapshot, get_fps_stats,
    get_gpu_info, get_network_stats, get_running_processes,
    get_display_info, get_disk_io, get_top_apps, batch_adb_snapshot
)

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# SYSTEM PROMPT (kept — used by both old and new flows)
# ═══════════════════════════════════════════════════════════════
SYSTEM_PROMPT = """
You are an expert Android Performance Analyst integrated into a real-time MCP (Model Context Protocol) diagnostic system.

You are directly connected to a live Android device via ADB through an MCP server. You have access to real-time diagnostic tools
that pull live telemetry from the actual device. This is NOT simulated data — it is live hardware data.

Your responsibilities:
1. **Always use your tools to get real-time data** — call the appropriate tool(s) before answering. Never guess or make up values.
2. **Identify bottlenecks** — thermal throttling, memory pressure, GPU frequency drops, high CPU, jank frames.
3. **Explain impact on gameplay** — connect metrics to user-visible effects (stutters, FPS drops, input lag).
4. **Give actionable fixes** — concrete steps like "lower resolution", "close background apps", "enable battery saver".
5. **Cross-correlate metrics** — e.g., high thermal + dropping GPU clock = thermal throttle causing FPS drops.
6. **Report device state honestly** — if the game is not in the foreground, say so. If data is unavailable, note it.

IMPORTANT: You are talking directly to the user who is playing a game on their Android phone.
Be CONCISE, TECHNICAL, and always ground your analysis in the ACTUAL DATA from the tools.
Do NOT make up data. Only analyze what the device tools provide.
"""

# ═══════════════════════════════════════════════════════════════
# LEGACY FUNCTIONS (kept for Real-Time Monitor + backward compat)
# ═══════════════════════════════════════════════════════════════

def get_full_realtime_snapshot(package_name):
    """Deep optimization: use a single batched ADB call to pull all metrics in one go."""
    return batch_adb_snapshot(package_name)

def analyze_performance_engine(package_name):
    """Legacy analysis engine — kept for fallback."""
    data = collect_snapshot(package_name, datetime.now().isoformat())
    current_pkg = get_foreground_pkg()
    issues = []
    status = "running"

    if current_pkg != package_name:
        status = "not_running_or_background"
        issues.append(f"Game is not in foreground (Current: {current_pkg})")

    if data.get("cpu_pct", 0) > 80: issues.append("High CPU usage")
    if data.get("battery_temp") and data["battery_temp"] > 40: issues.append("Thermal throttling risk")
    if data.get("mem_pss_mb", 0) > 3000: issues.append("High Memory usage (RAM bottleneck)")

    fps = get_fps_stats(package_name)
    if status == "running" and fps.get("janky_frames", 0) > 5:
        issues.append("High frame drops (jank detected)")

    gpu = get_gpu_info()
    display = get_display_info()
    network = get_network_stats()

    return {
        "status": status,
        "foreground_app": current_pkg,
        "raw_snapshot": data,
        "fps_data": fps,
        "gpu": gpu,
        "display": display,
        "network": network,
        "diagnosed_issues": issues if issues else ["No major bottlenecks detected. Device is running smoothly."]
    }

# Legacy keyword-based routing — kept as fallback
AVAILABLE_TOOLS = {
    "get_cpu_usage":       {"function": lambda p: get_cpu(p),               "description": "Get current CPU % load for the game.",            "keywords": ["cpu", "load", "processor"]},
    "get_memory_usage":    {"function": lambda p: get_memory(p),            "description": "Get RAM/Memory PSS usage.",                       "keywords": ["ram", "memory", "leak", "oom"]},
    "get_fps":             {"function": lambda p: get_fps_stats(p),         "description": "Get total frames and janky stutters.",            "keywords": ["fps", "jank", "stutter", "lag", "frames", "smooth", "framerate"]},
    "get_thermal":         {"function": lambda _: get_thermals(),           "description": "Get max thermal zone temperature.",               "keywords": ["temp", "thermal", "overheating", "hot", "throttle", "heat"]},
    "get_battery":         {"function": lambda _: get_battery(),            "description": "Get battery %, temperature, voltage.",            "keywords": ["battery", "power", "charge", "drain"]},
    "get_gpu":             {"function": lambda _: get_gpu_info(),           "description": "Get GPU clock, busy %, and governor.",            "keywords": ["gpu", "graphics", "render", "adreno", "mali"]},
    "get_network":         {"function": lambda _: get_network_stats(),      "description": "Get network type, WiFi signal, link speed.",      "keywords": ["network", "wifi", "ping", "signal", "internet", "latency", "connection"]},
    "get_display":         {"function": lambda _: get_display_info(),       "description": "Get screen resolution, density, refresh rate.",   "keywords": ["display", "screen", "resolution", "refresh", "brightness"]},
    "get_processes":       {"function": lambda p: get_running_processes(p), "description": "Get top processes and foreground app.",           "keywords": ["process", "running", "foreground", "background", "app", "kill"]},
    "get_disk":            {"function": lambda _: get_disk_io(),            "description": "Get storage usage and disk I/O.",                "keywords": ["disk", "storage", "space", "io"]},
    "get_top_apps":        {"function": lambda _: get_top_apps(),           "description": "Get recently used applications.",                "keywords": ["recent", "apps", "open", "switch"]},
    "analyze_performance": {"function": lambda p: analyze_performance_engine(p), "description": "Run the AI Lag Diagnosis Engine to detect bottlenecks.", "keywords": ["diagnose", "analyze", "why", "lagging", "issue", "problem", "slow", "bad", "performance", "check", "report", "status"]}
}

def route_query(query, pkg):
    """Legacy keyword-based routing — kept as fallback if MCP fails."""
    query_lower = query.lower()
    matched_tools = [name for name, info in AVAILABLE_TOOLS.items() if any(kw in query_lower for kw in info["keywords"])]
    if not matched_tools: matched_tools = ["analyze_performance"]

    results = {}
    for tool_name in matched_tools:
        results[tool_name] = AVAILABLE_TOOLS[tool_name]["function"](pkg)
    return results, matched_tools

def format_ai_response(results, tools_called, live_snapshot=None):
    """Legacy formatting — kept as fallback."""
    analysis = results.get("analyze_performance", {})
    status = analysis.get("status", "unknown")
    foreground = analysis.get("foreground_app", "unknown")

    parts = [
        "🤖 **Android Performance Analyst — Live Device Report**\n",
        f"📱 Foreground App: `{foreground}`",
        f"🎮 Game Status: **{status.upper()}**\n",
    ]

    if live_snapshot:
        batt = live_snapshot.get('battery', {})
        cpu = live_snapshot.get('cpu', {})
        mem = live_snapshot.get('memory', {})
        therm = live_snapshot.get('thermals', {})
        disp = live_snapshot.get('display', {})
        gpu = live_snapshot.get('gpu', {})
        net = live_snapshot.get('network', {})
        parts.append("**📊 Real-Time Device Snapshot:**")
        parts.append(f"- 🔋 Battery: {batt.get('battery_pct', 'N/A')}% | Temp: {batt.get('battery_temp', 'N/A')}°C")
        parts.append(f"- ⚡ CPU: {cpu.get('cpu_pct', 0):.1f}% | GPU Clock: {gpu.get('gpu_clock_hz', 'N/A')}")
        parts.append(f"- 💾 RAM (PSS): {mem.get('mem_pss_mb', 0):.0f} MB")
        parts.append(f"- 🌡️ Max Thermal: {therm.get('max_thermal_c', 'N/A')}°C")
        parts.append(f"- 🖥️ Display: {disp.get('resolution', 'N/A')} @ {disp.get('refresh_rate_hz', 'N/A')} fps")
        parts.append(f"- 🌐 Network: {net.get('network_type', 'N/A')} | Signal: {net.get('wifi_rssi_dbm', 'N/A')} dBm\n")

    parts.append(f"*Tools executed:* `{'`, `'.join(tools_called)}`\n")
    parts.append("---")
    parts.append("*Raw Diagnostic Data:*")
    parts.append(f"```json\n{json.dumps(results, indent=2)}\n```")
    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════
# NEW: MCP CLIENT — Connects to mcp_server.py via stdio
# ═══════════════════════════════════════════════════════════════

def _get_server_params():
    """Get the StdioServerParameters to launch mcp_server.py."""
    python_exe = sys.executable  # Use the same Python interpreter
    server_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_server.py")
    return StdioServerParameters(
        command=python_exe,
        args=[server_script],
        env=None
    )


async def _discover_tools():
    """Connect to MCP server and list all available tools."""
    server_params = _get_server_params()
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools_result = await session.list_tools()
            return tools_result.tools


async def _call_mcp_tool(tool_name: str, arguments: dict):
    """Connect to MCP server and call a single tool."""
    server_params = _get_server_params()
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)
            return result


async def _call_mcp_tools_batch(tool_calls: list):
    """Connect to MCP server once and execute multiple tool calls in sequence.
    
    Args:
        tool_calls: list of dicts with 'name' and 'arguments' keys
    
    Returns:
        dict mapping tool call IDs to results
    """
    server_params = _get_server_params()
    results = {}
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for tc in tool_calls:
                try:
                    result = await session.call_tool(tc["name"], tc["arguments"], read_timeout_seconds=timedelta(seconds=60))
                    # Extract text content from MCP result
                    content = ""
                    if hasattr(result, 'content') and result.content:
                        for block in result.content:
                            if hasattr(block, 'text'):
                                content = block.text
                                break
                    results[tc["id"]] = content or str(result)
                except Exception as e:
                    logger.error(f"MCP tool call failed: {tc['name']}: {e}")
                    results[tc["id"]] = json.dumps({"error": str(e)})
    return results


def mcp_tools_to_openai_format(mcp_tools) -> list:
    """Convert MCP tool definitions to OpenAI/Groq function calling format."""
    openai_tools = []
    for tool in mcp_tools:
        # Build parameters from MCP tool's inputSchema
        params = {"type": "object", "properties": {}, "required": []}
        if hasattr(tool, 'inputSchema') and tool.inputSchema:
            schema = tool.inputSchema
            params["properties"] = schema.get("properties", {})
            params["required"] = schema.get("required", [])

        openai_tools.append({
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": params
            }
        })
    return openai_tools


def _run_async(coro):
    """Run an async coroutine from sync code, handling Streamlit's event loop."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


def discover_tools_sync():
    """Synchronous wrapper to discover MCP tools. Returns (mcp_tools, openai_format_tools)."""
    try:
        mcp_tools = _run_async(_discover_tools())
        openai_tools = mcp_tools_to_openai_format(mcp_tools)
        return mcp_tools, openai_tools
    except Exception as e:
        logger.error(f"Failed to discover MCP tools: {e}", exc_info=True)
        return [], []


def call_mcp_tools_sync(tool_calls: list) -> dict:
    """Synchronous wrapper to call multiple MCP tools.
    
    Args:
        tool_calls: list of dicts with 'id', 'name', and 'arguments' keys
    
    Returns:
        dict mapping tool call IDs to result strings
    """
    try:
        results = _run_async(_call_mcp_tools_batch(tool_calls))
        return results
    except Exception as e:
        logger.error(f"MCP tool execution failed: {e}", exc_info=True)
        return {tc["id"]: json.dumps({"error": str(e)}) for tc in tool_calls}


def chat_with_mcp(user_query: str, package_name: str, chat_history: list,
                  groq_api_key: str, status_callback=None) -> tuple:
    """Main AI Chat function using MCP Server + Groq Function Calling.
    
    Flow:
    1. Discover tools from MCP server
    2. Send user query + tool defs to Groq
    3. Groq decides which tools to call
    4. Execute tool calls via MCP server
    5. Send results back to Groq
    6. Return final response
    
    Args:
        user_query: The user's question
        package_name: Target Android package
        chat_history: Previous chat messages
        groq_api_key: Groq API key
        status_callback: Optional callback for status updates (e.g. st.spinner text)
    
    Returns:
        (response_text, tools_called_names, tool_results_summary)
    """
    import openai
    
    tools_called = []
    tool_results = {}
    
    # Step 1: Discover MCP tools
    if status_callback:
        status_callback("🔌 Connecting to MCP Server — discovering tools...")
    
    mcp_tools, openai_tools = discover_tools_sync()
    
    if not openai_tools:
        # Fallback to legacy if MCP server is unreachable
        if status_callback:
            status_callback("⚠️ MCP Server unavailable — falling back to direct ADB...")
        results, tools = route_query(user_query, package_name)
        live = get_full_realtime_snapshot(package_name)
        return format_ai_response(results, tools, live), tools, results
    
    tool_names = [t["function"]["name"] for t in openai_tools]
    if status_callback:
        status_callback(f"✅ MCP connected — {len(openai_tools)} tools available")
    
    # Step 2: Send to Groq with function calling
    if status_callback:
        status_callback("🧠 Sending query to Groq AI with MCP tool definitions...")
    
    client = openai.OpenAI(api_key=groq_api_key, base_url="https://api.groq.com/openai/v1")
    
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT + f"\n\nThe user's target game package is: {package_name}"},
    ]
    # Add chat history (last 6 messages for context)
    for msg in chat_history[-6:]:
        messages.append({"role": msg["role"], "content": msg["content"]})
    
    messages.append({"role": "user", "content": user_query})
    
    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            tools=openai_tools,
            tool_choice="auto",
            max_tokens=1500,
            temperature=0.2
        )
    except Exception as e:
        logger.error(f"Groq API error: {e}")
        # Fallback to legacy
        results, tools = route_query(user_query, package_name)
        live = get_full_realtime_snapshot(package_name)
        return format_ai_response(results, tools, live), tools, results
    
    response_message = response.choices[0].message
    
    # Step 3: Check if Groq wants to call tools
    if response_message.tool_calls:
        if status_callback:
            tool_names_called = [tc.function.name for tc in response_message.tool_calls]
            status_callback(f"🔧 AI is calling MCP tools: {', '.join(tool_names_called)}")
        
        # Prepare tool call batch
        batch = []
        for tc in response_message.tool_calls:
            try:
                args = json.loads(tc.function.arguments) if tc.function.arguments else {}
            except json.JSONDecodeError:
                args = {}
            
            # Inject package_name if the tool expects it but AI didn't provide it
            func_def = next((t for t in openai_tools if t["function"]["name"] == tc.function.name), None)
            if func_def and "package_name" in func_def["function"]["parameters"].get("properties", {}):
                if "package_name" not in args:
                    args["package_name"] = package_name
            
            batch.append({
                "id": tc.id,
                "name": tc.function.name,
                "arguments": args
            })
            tools_called.append(tc.function.name)
        
        # Step 4: Execute tools via MCP
        if status_callback:
            status_callback(f"📡 Executing {len(batch)} tool(s) on Android device via MCP...")
        
        tool_results = call_mcp_tools_sync(batch)
        
        # Step 5: Send results back to Groq
        if status_callback:
            status_callback("⚡ Groq AI analyzing real-time device data...")
        
        # Build follow-up messages with tool results
        follow_up_messages = messages + [response_message]
        for tc in response_message.tool_calls:
            result_content = tool_results.get(tc.id, '{"error": "no result"}')
            follow_up_messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result_content
            })
        
        try:
            final_response = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=follow_up_messages,
                max_tokens=1500,
                temperature=0.2
            )
            response_text = final_response.choices[0].message.content
        except Exception as e:
            logger.error(f"Groq follow-up error: {e}")
            response_text = f"⚠️ AI analysis failed: {e}\n\n**Raw tool data:**\n```json\n{json.dumps(tool_results, indent=2)}\n```"
    else:
        # Groq responded directly without calling tools
        response_text = response_message.content or "I couldn't generate a response. Please try again."
    
    return response_text, tools_called, tool_results
