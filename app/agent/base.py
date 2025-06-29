"""RestaurantRecommender agent implementation."""
import os
from typing import Dict, List, Optional, Union
from dotenv import load_dotenv
import uuid
import logging
import json
from decimal import Decimal

from langchain_openai import ChatOpenAI
from langchain.agents import AgentExecutor, create_react_agent
from langchain_core.prompts import PromptTemplate
from langchain_core.callbacks import BaseCallbackHandler
from pydantic import BaseModel

from .tools.tools import get_restaurant_tools
from .character.character import RestaurantRecommenderCharacter
# from .safety.validator import SafetyValidator
# from .operations.restaurant import RestaurantOperations
from ..commands.parser import CommandParser
from ..commands.models import (
    RestaurantCommand, RestaurantQuery, SearchCommand, 
    RecommendationCommand, InformationalCommand
)
from ..models.restaurant import AgentResponse
from app.config.config import OpenAIConfig, RestaurantAPIConfig

logger = logging.getLogger(__name__)

class OpenAILoggingHandler(BaseCallbackHandler):
    """Callback handler for logging OpenAI interactions."""
    
    def on_llm_start(self, serialized, prompts, **kwargs):
        """Log when LLM starts generating."""
        logger.info(f"\n{'='*50}\nLLM Request:")
        for i, prompt in enumerate(prompts):
            logger.info(f"Prompt {i}:\n{prompt}\n")
    
    def on_llm_end(self, response, **kwargs):
        """Log when LLM finishes generating."""
        logger.info(f"\nLLM Response:")
        try:
            # Log response type for debugging
            logger.info(f"Response type: {type(response)}")
            
            # Handle different response types more robustly
            if hasattr(response, 'generations') and response.generations:
                # LLMResult object - get the first generation
                logger.info("Handling LLMResult object")
                generation = response.generations[0][0]
                
                if hasattr(generation, 'message'):
                    # AIMessage within generation
                    message = generation.message
                    logger.info(f"Found message in generation: {type(message)}")
                    
                    # Log function calls if present
                    if hasattr(message, 'additional_kwargs') and message.additional_kwargs:
                        if 'function_call' in message.additional_kwargs:
                            func_call = message.additional_kwargs['function_call']
                            logger.info(f"Function Call:\n  Name: {func_call.get('name')}\n  Arguments: {func_call.get('arguments')}")
                        if 'tool_calls' in message.additional_kwargs:
                            tool_calls = message.additional_kwargs['tool_calls']
                            logger.info(f"Tool calls: {json.dumps(tool_calls, indent=2)}")
                    
                    # Log content if present
                    if hasattr(message, 'content') and message.content:
                        logger.info(f"Response content: {message.content}")
                    else:
                        logger.info(f"Message object: {str(message)}")
                        
                elif hasattr(generation, 'text'):
                    # Simple text generation
                    logger.info(f"Response text: {generation.text}")
                elif hasattr(generation, 'content'):
                    logger.info(f"Generation content: {generation.content}")
                else:
                    logger.info(f"Generation object: {str(generation)}")
                    
            elif hasattr(response, 'additional_kwargs'):
                # Direct AIMessage or similar
                logger.info("Handling direct message response")
                
                # Log function calls if present
                if response.additional_kwargs:
                    if 'function_call' in response.additional_kwargs:
                        func_call = response.additional_kwargs['function_call']
                        logger.info(f"Function Call:\n  Name: {func_call.get('name')}\n  Arguments: {func_call.get('arguments')}")
                    if 'tool_calls' in response.additional_kwargs:
                        tool_calls = response.additional_kwargs['tool_calls']
                        logger.info(f"Tool calls: {json.dumps(tool_calls, indent=2)}")
                
                if hasattr(response, 'content') and response.content:
                    logger.info(f"Response content: {response.content}")
                else:
                    logger.info(f"Response object: {str(response)}")
                    
            else:
                # Fallback - log what we can safely
                logger.info("Fallback response handling")
                
                # Try to serialize the response
                if hasattr(response, 'model_dump'):
                    try:
                        logger.info(f"Full Response:\n{json.dumps(response.model_dump(), indent=2)}")
                    except Exception as serialize_error:
                        logger.info(f"Could not serialize response: {serialize_error}")
                        logger.info(f"Response string: {str(response)}")
                elif hasattr(response, 'dict'):
                    try:
                        logger.info(f"Full Response:\n{json.dumps(response.dict(), indent=2)}")
                    except Exception as serialize_error:
                        logger.info(f"Could not serialize response: {serialize_error}")
                        logger.info(f"Response string: {str(response)}")
                else:
                    logger.info(f"Response: {str(response)}")
                
        except Exception as e:
            logger.error(f"Error logging response: {e}")
            logger.error(f"Response type: {type(response)}")
            logger.error(f"Response attributes: {dir(response) if hasattr(response, '__dict__') else 'No attributes'}")
            logger.info(f"Raw Response: {response}")
        
        logger.info(f"{'='*50}\n")
    
    def on_llm_error(self, error, **kwargs):
        """Log when LLM errors."""
        logger.error(f"\nLLM Error: {str(error)}")
        
    def on_tool_start(self, serialized, input_str, **kwargs):
        """Log when a tool starts."""
        logger.info(f"\nTool Start: {serialized.get('name', 'Unknown Tool')}")
        logger.info(f"Input: {input_str}")
        
    def on_tool_end(self, output, **kwargs):
        """Log when a tool ends."""
        logger.info(f"\nTool Output: {output}")
        
    def on_tool_error(self, error, **kwargs):
        """Log when a tool errors."""
        logger.error(f"\nTool Error: {str(error)}")

    def on_chain_start(self, serialized, inputs, **kwargs):
        """Log when a chain starts."""
        logger.info(f"\nChain Start: {serialized.get('name', 'Unknown Chain')}")
        logger.info(f"Inputs: {json.dumps(inputs, indent=2)}")

    def on_chain_end(self, outputs, **kwargs):
        """Log when a chain ends."""
        logger.info(f"\nChain Output:")
        logger.info(f"Outputs: {json.dumps(outputs, indent=2)}")

    def on_agent_action(self, action, **kwargs):
        """Log agent actions."""
        logger.info(f"\nAgent Action:")
        logger.info(f"Tool: {action.tool}")
        logger.info(f"Input: {action.tool_input}")
        logger.info(f"Thought: {action.log}")

    def on_agent_finish(self, finish, **kwargs):
        """Log agent finish."""
        logger.info(f"\nAgent Finish:")
        logger.info(f"Output: {finish.return_values}")
        logger.info(f"Log: {finish.log}")


# Create prompt template for the agent
AGENT_PROMPT = PromptTemplate.from_template(
    """You are a Restaurant Recommender assistant. Help users find restaurants and create collections.

    Available tools:
    {tools}
    
    Context (if available): {context}
    
    CRITICAL COLLECTION CREATION RULES:
    1. If you see "Restaurant IDs for Collection:" in the context, you MUST use create_collection_with_restaurants
    2. If the context contains restaurant IDs in brackets like ["id1", "id2"], use create_collection_with_restaurants
    3. Only use create_collection for empty collections without restaurants
    
    Instructions:
    1. For restaurant searches, use the search_restaurants tool
    2. For creating collections WITH restaurants, use create_collection_with_restaurants tool
    3. For creating empty collections, use the create_collection tool
    4. Always check the context for restaurant IDs before creating collections
    5. Extract collection names from user requests (look for "called", "named", etc.)
    6. Format responses clearly and be helpful
    
    COLLECTION WITH RESTAURANTS: When you see restaurant IDs in the context:
    - ALWAYS use create_collection_with_restaurants (never create_collection)
    - Copy the restaurant_ids array EXACTLY from the context
    - GENERATE A UNIQUE COLLECTION NAME based on the search context, cuisines, and locations
    - If user specified a name, use it; otherwise create a descriptive name with timestamp
    - Include ALL required fields: name, description, restaurant_ids, auth_token, is_public, tags
    - Use the auth_token from the context
    - Set is_public to true unless specified otherwise
    - Add tags like ["user_created", "restaurant_search"]
    
    COLLECTION NAME EXAMPLES:
    - "Italian Gems in Delhi - 20241220_1430"
    - "Best Pizza Spots Found 20241220_1430"
    - "Romantic Dinner Collection - 20241220_1430"
    - "Budget Friendly Eats 20241220_1430"
    
    JSON FORMAT: Always use proper JSON format with double quotes for strings and arrays.
    Do NOT wrap JSON in markdown code blocks or backticks. Provide raw JSON only.
    Example: {{"name": "Collection Name", "description": "Description", "restaurant_ids": ["id1", "id2"], "auth_token": "token", "is_public": true, "tags": ["tag1"]}}
    
    Use this format:
    
    Question: {input}
    Thought: I need to analyze what the user wants and check for context about restaurants
    Action: [choose from: {tool_names}]
    Action Input: [provide the required parameters as valid JSON with double quotes]
    Observation: [result from the tool]
    Thought: Now I can provide the answer based on the result
    Final Answer: [clear, helpful response to the user]
    
    Begin!
    
    Question: {input}
    Thought:{agent_scratchpad}"""
)


class AgentState(BaseModel):
    """State of the agent.
    
    Attributes:
        messages: List of message dictionaries
        thread_id: Unique identifier for the conversation
        output: Optional output from the agent
    """
    messages: List[Dict[str, str]]
    thread_id: str
    output: Optional[str] = None


class RestaurantRecommenderAgent:
    """RestaurantRecommender agent for optimizing staking rewards.
    
    This agent uses LangChain's function calling and React framework to handle
    staking operations. It processes natural language commands into strongly-typed
    command models and executes them using appropriate tools.
    
    Attributes:
        llm: Language model for agent
        character: Agent character definition
        validator: Safety validator
        operations: Restaurant operations handler
        command_parser: Command parser for natural language input
        tools: List of available tools
        agent: React agent instance
        agent_executor: Agent executor instance
    """

    def __init__(
        self,
        model_name: str = OpenAIConfig.MODEL_NAME,
        temperature: float = OpenAIConfig.AGENT_TEMPERATURE,
        command_parser: Optional[CommandParser] = None,
        memory: Optional = None,
        # safety_validator: Optional[SafetyValidator] = None,
    ):
        """Initialize the RestaurantRecommender agent.

        Args:
            model_name: Name of the OpenAI model to use
            temperature: Temperature setting for the model
            command_parser: Optional command parser instance
            safety_validator: Optional safety validator instance
        """
        # Load environment variables
        load_dotenv()

        # Initialize components
        self.llm = ChatOpenAI(
            model_name=model_name,
            temperature=temperature,
            callbacks=[OpenAILoggingHandler()],
            api_key=OpenAIConfig.API_KEY,
            base_url=OpenAIConfig.BASE_URL,
            request_timeout=15,  # Reduced timeout to 15 seconds
            max_retries=0,  # No retries to prevent delays
            streaming=False  # Disable streaming for more predictable responses
        )
        
        

        self.character = RestaurantRecommenderCharacter()       
        # self.validator = safety_validator or SafetyValidator()
        # self.operations = RestaurantOperations()
        self.command_parser = command_parser or CommandParser()
        self.memory = memory  # Store memory instance for context
        
        # Create agent with tools
        self.tools = self._get_tools()
        self.agent = create_react_agent(self.llm, self.tools, AGENT_PROMPT)
        self.agent_executor = AgentExecutor(
            agent=self.agent,
            tools=self.tools,
            memory=self.memory,  # Add memory to agent executor
            handle_parsing_errors=True,  # Handle cases where LLM output includes both action and final answer
            max_iterations=5,  # Limit iterations to prevent infinite loops
            max_execution_time=20,  # Maximum execution time in seconds
            return_intermediate_steps=False,  # Don't return intermediate steps to reduce response size
            verbose=True  # Enable verbose logging for debugging
        )

    def _get_tools(self):
        """Get the list of tools available to the agent.
    
        Returns:
            List of LangChain tools for restaurant operations.
        """
        return get_restaurant_tools()

    def _validate_request(self, state: AgentState) -> bool:
        """Validate a user request.

        Args:
            state: Current agent state

        Returns:
            Whether the request is valid
        """
        # Validator is currently commented out, so always return True for now
        # TODO: Implement proper validation when SafetyValidator is available
        return True
        # request = state.messages[-1]["content"]
        # is_valid, reason = self.validator.validate_request(request)
        # if not is_valid:
        #     state.output = reason
        # return is_valid

    async def invoke(self, state: AgentState) -> AgentState:
        """Invoke the agent with timeout protection.

        Args:
            state: Initial agent state

        Returns:
            Final agent state
        """
        try:
            if not self._validate_request(state):
                error_message = "Request validation failed"
                state.output = error_message
                return state

            # Convert state to format expected by agent
            input_dict = {
                "input": state.messages[0]["content"],
                "agent_scratchpad": "",
                "tools": "\n".join(f"{tool.name}: {tool.description}" for tool in self.tools),
                "tool_names": ", ".join(tool.name for tool in self.tools),
                "thread_id": state.thread_id  # Include thread_id for memory context
            }
            
            # Add memory context if available
            if self.memory:
                try:
                    memory_vars = self.memory.load_memory_variables(input_dict)
                    # Add conversation history and context to the input
                    if memory_vars.get("enhanced_context"):
                        input_dict["context"] = memory_vars["enhanced_context"]
                except Exception as e:
                    logger.warning(f"Failed to load memory context: {e}")
                    # Continue without memory context

            logger.info(f"Invoking agent with input: {input_dict['input']}")
            
            # Run agent with timeout protection
            import asyncio
            try:
                response = await asyncio.wait_for(
                    self.agent_executor.ainvoke(input_dict), 
                    timeout=20.0  # 20 second timeout
                )
                state.output = response.get("output", "No output generated")
                logger.info(f"Agent response generated successfully")
                return state
            except asyncio.TimeoutError:
                error_msg = "Agent execution timed out after 20 seconds"
                logger.error(error_msg)
                state.output = error_msg
                return state
                
        except Exception as e:
            error_msg = f"Error processing request: {str(e)}"
            logger.error(error_msg, exc_info=True)
            state.output = error_msg
            return state

    async def handle_request(self, request: str) -> AgentResponse:
        """Handle a user request using LLM-based agent.
        
        Args:
            request: User's request string
            
        Returns:
            AgentResponse containing the result
        """
        try:
            logger.info(f"Processing request with agent: {request}")
            
            # Create agent state
            state = AgentState(
                messages=[{"role": "user", "content": request}],
                thread_id=str(uuid.uuid4())
            )
            
            # Use the conversational agent instead of command parser
            result_state = await self.invoke(state)
            
            if result_state.output:
                # Try to parse the original request to get command info
                try:
                    command = self.command_parser.parse_request(request)
                    response = AgentResponse(success=True, message=result_state.output)
                    response.parsed_command = command
                    return response
                except:
                    # If parsing fails, return generic response
                    return AgentResponse(success=True, message=result_state.output)
            else:
                return AgentResponse(
                    success=False,
                    message="No response generated",
                    error="Agent did not generate output"
                )
            
        except Exception as e:
            logger.error(f"Error handling request: {str(e)}", exc_info=True)
            return AgentResponse(
                success=False,
                message=f"Error processing request: {str(e)}",
                error=str(e)
            )

    async def execute_command(self, command) -> AgentResponse:
        """Execute a parsed command.
        
        Args:
            command: The command to execute
            
        Returns:
            AgentResponse: Response with success status and message
        """
        try:
            if isinstance(command, SearchCommand):
                # Execute restaurant search
                query = command.search_query
                logger.info(f"Executing search command: query='{query.query}', place='{query.place}'")
                
                # For now, return a mock response with the parsed information
                response_msg = f"🔍 **Searching for restaurants...**\n\n"
                response_msg += f"**Query:** {query.query}\n"
                
                if query.place:
                    response_msg += f"**Location:** {query.place}\n"
                if query.cuisine:
                    response_msg += f"**Cuisine:** {query.cuisine}\n"
                if query.price_range:
                    response_msg += f"**Price Range:** {query.price_range}\n"
                if query.dietary_restrictions:
                    response_msg += f"**Dietary Restrictions:** {query.dietary_restrictions}\n"
                
                # Mock restaurant results based on the query
                if "butter chicken" in query.query.lower():
                    response_msg += f"\n🍽️ **Top Results:**\n\n"
                    response_msg += f"1. **Karim's** - {query.place or 'Delhi'}\n"
                    response_msg += f"   ⭐ 4.2/5 | Indian Cuisine | Moderate Price\n"
                    response_msg += f"   Famous for authentic butter chicken and mughlai cuisine\n\n"
                    
                    response_msg += f"2. **Punjabi By Nature** - {query.place or 'Multiple Locations'}\n"
                    response_msg += f"   ⭐ 4.0/5 | North Indian | Mid-Range\n"
                    response_msg += f"   Known for rich, creamy butter chicken\n\n"
                    
                    response_msg += f"3. **Moti Mahal Delux** - {query.place or 'Delhi'}\n"
                    response_msg += f"   ⭐ 4.1/5 | Indian | Moderate\n"
                    response_msg += f"   Legendary restaurant, birthplace of butter chicken\n"
                else:
                    # Generic restaurant search response
                    response_msg += f"\n🍽️ **Found restaurants matching your search!**\n\n"
                    response_msg += f"Here are some great options in {query.place or 'your area'} for {query.query}.\n"
                    response_msg += f"I'd be happy to provide more specific recommendations if you can tell me more about what you're looking for!"
                
                return AgentResponse(success=True, message=response_msg)
                
            elif isinstance(command, RecommendationCommand):
                # Execute restaurant recommendation
                query = command.recommendation_query
                logger.info(f"Executing recommendation command: query='{query.query}', place='{query.place}'")
                
                response_msg = f"🎯 **Restaurant Recommendations**\n\n"
                response_msg += f"Based on your request: '{query.query}'\n"
                
                if query.place:
                    response_msg += f"Location: {query.place}\n\n"
                
                response_msg += f"Here are some great options I'd recommend:\n\n"
                response_msg += f"• Look for highly-rated local favorites\n"
                response_msg += f"• Consider trying authentic cuisine specific to {query.place or 'the area'}\n"
                response_msg += f"• Check recent reviews for current quality\n\n"
                response_msg += f"Would you like me to search for something more specific?"
                
                return AgentResponse(success=True, message=response_msg)
                
            elif isinstance(command, InformationalCommand):
                # Handle info request
                if command.topic == "help":
                    # Return a more detailed help message
                    help_msg = """I can help you find great restaurants! I can:

- Search for restaurants by location and cuisine
- Find popular dining spots  
- Recommend places based on your preferences
- Provide restaurant details and information
- Help with specific food cravings like "best butter chicken"

Just ask me what you're looking for!"""
                    return AgentResponse(success=True, message=help_msg)
                else:
                    return AgentResponse(success=True, message=self.character.format_response(command.topic))
                
            else:
                return AgentResponse(
                    success=False,
                    message=f"Unknown command type: {type(command)}",
                    error="Invalid command type"
                )
                
        except Exception as e:
            logger.error(f"Error executing command: {str(e)}")
            return AgentResponse(success=False, message=str(e), error=str(e))

