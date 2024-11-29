import os
import logging
import psycopg2
import datetime
# from dotenv import load_dotenv
from telegram.ext import (
    Updater,
    CommandHandler,
    MessageHandler,
    Filters,
    ConversationHandler,
)
from telegram import ReplyKeyboardRemove, ParseMode
import asyncio
import requests  # Import requests for HTTP requests

# Import necessary Solana libraries
from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey
from spl.token.instructions import get_associated_token_address

# Load environment variables
# load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

# Solana configuration
SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"
ROCKAWAY_TOKEN_MINT = "9xUY3nsESFBD6q3wsMAH8Ue287VYP5feqyNNE2jopump"
TREASURY_WALLET = "ENChan5dTdnFDRn6xVAivPbvooeckPHagMA6388zFpLF"
ROCKAWAY_TOKEN_MINT_PUBKEY = Pubkey.from_string(ROCKAWAY_TOKEN_MINT)
TREASURY_WALLET_PUBKEY = Pubkey.from_string(TREASURY_WALLET)
TOKEN_DECIMALS = 6  # Adjust based on your token's decimals
REQUIRED_TOKENS = 2_000_000 * (10 ** TOKEN_DECIMALS)  # Adjusted token amount considering decimals

# Instantiate OpenAI async client
from openai import AsyncOpenAI

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# Instantiate Solana async client
solana_client = AsyncClient(SOLANA_RPC_URL)

# Set up logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Database connection
conn = psycopg2.connect(DATABASE_URL)
cursor = conn.cursor()

# Update table schema
create_table_query = """
CREATE TABLE IF NOT EXISTS pitches (
    user_id BIGINT PRIMARY KEY,
    username TEXT,
    context_history TEXT,
    evaluation TEXT,
    approved BOOLEAN,
    payment_confirmed BOOLEAN DEFAULT FALSE,
    pitch_start_time TIMESTAMP,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""
cursor.execute(create_table_query)
conn.commit()

# Define conversation states
(
    PAYMENT,
    WAITING_FOR_WALLET_ADDRESS,
    QUESTION_1,
    QUESTION_2,
    QUESTION_3,
    QUESTION_4,
    QUESTION_5,
    QUESTION_6,
) = range(8)

# List of questions
questions = [
    "What problem does your project aim to solve?",
    "Who is your target audience? How will you acquire them?",
    "What is your unique value proposition?",
    "Do you have a prototype or MVP? If so, do you have any traction?",
    "Tell me about your team and their backgrounds. Have you worked together before?",
    "How much are you looking to raise? Do you have a lead investor?",
]


def start(update, context):
    welcome_message = (
        "Hey, I'm Rockaway's new intern 🤓\n\n"
        "They're making me hear out your pitch...\n\n"
        "If I like it, you might just get a 30-minute call with our VC team.\n\n"
        "Ready to dive in? Type /pitch to get started."
    )
    update.message.reply_text(welcome_message)


def help_command(update, context):
    help_text = (
        "Available commands:\n"
        "/start - Welcome message\n"
        "/pitch - Begin pitching your project\n"
        "/check - Check payment status\n"
        "/help - Show this help message\n"
        "/cancel - Cancel the pitching process"
    )
    update.message.reply_text(help_text)


def pitch_start(update, context):
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.full_name

    # Initialize user data
    context.user_data.clear()
    context.user_data["answers"] = []
    context.user_data["context_history"] = ""
    context.user_data["user_id"] = user_id
    context.user_data["username"] = username
    context.user_data["pitch_start_time"] = datetime.datetime.utcnow()

    # Save pitch start time to database
    cursor.execute(
        """
        INSERT INTO pitches (user_id, username, pitch_start_time)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET
        pitch_start_time = EXCLUDED.pitch_start_time,
        payment_confirmed = FALSE
    """,
        (
            user_id,
            username,
            context.user_data["pitch_start_time"],
        ),
    )
    conn.commit()

    update.message.reply_text(
        f"You think I do this for free? You dreamin...\n\n"
        f"Go buy *2,000,000 Rockaway tokens* and send them to our treasury wallet:\n`{TREASURY_WALLET}`\n\n"
        f"The contract address for the tokens is:\n`{ROCKAWAY_TOKEN_MINT}`\n\n"
        f"If you can't figure this out, you're ngmi.",
        parse_mode=ParseMode.MARKDOWN,
    )

    update.message.reply_text(
        "After sending the tokens, type /check to verify your payment."
    )

    return PAYMENT


def check_payment(update, context):
    user_data = context.user_data
    if "wallet_address" not in user_data:
        update.message.reply_text(
            "Please enter the wallet address you sent the tokens from:"
        )
        return WAITING_FOR_WALLET_ADDRESS
    else:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(verify_payment(update, context))
        loop.close()
        return result


def receive_wallet_address(update, context):
    user_data = context.user_data
    wallet_address = update.message.text.strip()
    # Validate wallet address format
    try:
        sender_wallet = Pubkey.from_string(wallet_address)
        user_data["wallet_address"] = sender_wallet  # Store as Pubkey object
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(verify_payment(update, context))
        loop.close()
        return result
    except Exception:
        update.message.reply_text(
            "Invalid wallet address. Please enter a valid Solana wallet address:"
        )
        return WAITING_FOR_WALLET_ADDRESS


async def verify_payment(update, context):
    user_data = context.user_data
    wallet_pubkey = user_data.get("wallet_address")
    user_id = user_data.get("user_id")
    pitch_start_time = user_data.get("pitch_start_time")

    try:
        # Get associated token account addresses as strings
        user_token_account_pubkey = get_associated_token_address(
            wallet_pubkey, ROCKAWAY_TOKEN_MINT_PUBKEY
        )
        treasury_token_account_pubkey = get_associated_token_address(
            TREASURY_WALLET_PUBKEY, ROCKAWAY_TOKEN_MINT_PUBKEY
        )
        user_token_account = str(user_token_account_pubkey)
        treasury_token_account = str(treasury_token_account_pubkey)

        logger.info(f"User token account: {user_token_account}")
        logger.info(f"Treasury token account: {treasury_token_account}")

        # Fetch recent signatures involving the treasury token account
        signatures_response = await solana_client.get_signatures_for_address(
            treasury_token_account_pubkey, limit=100
        )
        signatures = signatures_response.value

        if not signatures:
            logger.info("No signatures found for treasury token account.")
            update.message.reply_text(
                "Payment not yet received or insufficient tokens sent. Please check and try again."
            )
            return PAYMENT

        for sig_info in signatures:
            sig = str(sig_info.signature)  # Ensure sig is a string
            block_time = sig_info.block_time
            logger.info(f"Processing signature: {sig} at block time {block_time}")

            if block_time is None:
                continue
            tx_time = datetime.datetime.utcfromtimestamp(block_time)
            logger.info(
                f"Transaction time: {tx_time}, Pitch start time: {pitch_start_time}"
            )

            if tx_time < pitch_start_time:
                logger.info("Transaction is before pitch start time, skipping.")
                continue  # Ignore transactions before pitch started

            # Fetch transaction details using HTTP request
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTransaction",
                "params": [
                    sig,
                    {
                        "encoding": "jsonParsed",
                    },
                ],
            }
            response = requests.post(SOLANA_RPC_URL, json=payload)
            tx_data = response.json()

            if "result" not in tx_data or tx_data["result"] is None:
                logger.info("Transaction not found, skipping.")
                continue

            tx = tx_data["result"]
            transaction = tx["transaction"]
            message = transaction["message"]
            instructions = message["instructions"]

            for instruction in instructions:
                logger.info(f"Instruction: {instruction}")

                if instruction.get("program") != "spl-token":
                    logger.info("Instruction is not spl-token, skipping.")
                    continue
                parsed = instruction.get("parsed")
                if not parsed:
                    logger.info("No parsed data, skipping.")
                    continue
                info = parsed.get("info", {})
                instruction_type = parsed.get("type")
                logger.info(f"Instruction type: {instruction_type}")
                logger.info(f"Instruction info: {info}")  # Additional debugging

                if instruction_type in ["transfer", "transferChecked"]:
                    source = info.get("source")
                    destination = info.get("destination")
                    logger.info(f"Source: {source}, Destination: {destination}")

                    if source == user_token_account and destination == treasury_token_account:
                        token_amount_info = info.get("tokenAmount", {})
                        amount_str = token_amount_info.get("amount", "0")
                        amount = int(amount_str)
                        logger.info(f"Amount transferred: {amount}")

                        required_amount = REQUIRED_TOKENS
                        logger.info(f"Required amount: {required_amount}")

                        if amount >= required_amount:
                            logger.info("Payment confirmed.")
                            # Payment confirmed
                            update.message.reply_text(
                                "Payment received! Let's start your pitch!"
                            )
                            # Update payment status in the database
                            cursor.execute(
                                """
                                UPDATE pitches SET payment_confirmed = TRUE WHERE user_id = %s
                            """,
                                (user_id,),
                            )
                            conn.commit()
                            # Proceed to the first question
                            update.message.reply_text(
                                "Alright, let's go! 💡\n" + questions[0]
                            )
                            return QUESTION_1  # Start the conversation at QUESTION_1
                        else:
                            logger.info("Amount is less than required tokens.")
                else:
                    logger.info(f"Instruction type {instruction_type} is not handled.")

        update.message.reply_text(
            "Payment not yet received or insufficient tokens sent. Please check and try again."
        )
        return PAYMENT

    except Exception as e:
        logger.error("Error verifying payment: %s", str(e))
        update.message.reply_text(
            "An error occurred while verifying your payment. Please try again."
        )
        return PAYMENT


def handle_question(update, context):
    """Wrapper to handle the question flow synchronously."""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(handle_question_async(update, context))
        loop.close()
        return result
    except Exception as e:
        logger.error("Error in handle_question: %s", e)
        update.message.reply_text("An error occurred while processing your request. Please try again.")
        return ConversationHandler.END


async def handle_question_async(update, context):
    user_data = context.user_data
    answers = user_data["answers"]
    context_history = user_data.setdefault("context_history", "")
    question_index = len(answers)

    # Append the user's answer
    user_response = update.message.text
    answers.append(user_response)
    context_history += f"Question {question_index + 1}: {questions[question_index]}\n"
    context_history += f"Answer: {user_response}\n"

    # Save updated context_history
    user_data["context_history"] = context_history

    # Prepare AI prompt
    messages = [
        {
            "role": "system",
            "content": (
                "You are a sarcastic, witty VC intern evaluating a startup pitch. "
                "You're playful, but also cynical like Simon Cowell. "
                "Avoid repeating similar feedback or jokes. "
                "Keep track of the conversation so far and ensure your responses are varied."
            ),
        }
    ]

    # Build the conversation history
    exchanges = context_history.strip().split("\n")
    for exchange in exchanges:
        if exchange.startswith("Question"):
            role = "user"
            content = exchange.split(":", 1)[1].strip()
        elif exchange.startswith("Answer"):
            role = "user"
            content = exchange.split(":", 1)[1].strip()
        elif exchange.startswith("AI Response"):
            role = "assistant"
            content = exchange.replace("AI Response:", "").strip()
        else:
            continue
        messages.append({"role": role, "content": content})

    logger.info("Prepared messages for OpenAI API: %s", messages)

    try:
        response = await openai_client.chat.completions.create(
            model="gpt-4",
            messages=messages,
            temperature=0.7,
        )

        ai_reply = response.choices[0].message.content.strip()

        # Append AI's reply to context_history
        context_history += f"AI Response: {ai_reply}\n"
        user_data["context_history"] = context_history

        update.message.reply_text(ai_reply)

        # Move to the next question
        if len(answers) < len(questions):
            update.message.reply_text(questions[len(answers)])
            return QUESTION_1 + len(answers)  # Move to the next question state
        else:
            update.message.reply_text(
                "That’s it for the questions! Let me process your responses..."
            )
            await evaluate_pitch(update, context)
            return ConversationHandler.END

    except Exception as e:
        logger.error("Error during OpenAI API call: %s", str(e))
        update.message.reply_text(
            "An error occurred while communicating with the AI. Please try again."
        )
        return ConversationHandler.END


async def evaluate_pitch(update, context):
    user_data = context.user_data
    context_history = user_data.get("context_history", "")

    if not context_history.strip():
        update.message.reply_text(
            "It seems there was no context to evaluate. Please try again."
        )
        return

    messages = [
        {
            "role": "system",
            "content": (
                "You are a VC intern tasked with evaluating startup pitches. "
                "Based on the following conversation, provide an overall evaluation. "
                "Include a decision ('Approved' or 'Not Approved') and a brief explanation."
            ),
        }
    ]

    # Build the conversation history
    exchanges = context_history.strip().split("\n")
    for exchange in exchanges:
        if exchange.startswith("AI Response"):
            role = "assistant"
            content = exchange.replace("AI Response:", "").strip()
        else:
            role = "user"
            content = exchange.split(":", 1)[1].strip() if ":" in exchange else exchange
        messages.append({"role": role, "content": content})

    logger.info("Prepared evaluation messages for OpenAI API: %s", messages)

    try:
        response = await openai_client.chat.completions.create(
            model="gpt-4",
            messages=messages,
            temperature=0.7,
        )

        evaluation = response.choices[0].message.content.strip()
        approved = "Approved" in evaluation and "Not Approved" not in evaluation

        # Save to database
        cursor.execute(
            """
            UPDATE pitches SET context_history = %s, evaluation = %s, approved = %s, timestamp = CURRENT_TIMESTAMP
            WHERE user_id = %s
        """,
            (
                context_history,
                evaluation,
                approved,
                user_data["user_id"],
            ),
        )
        conn.commit()

        if approved:
            update.message.reply_text(
                "Congratulations! 🎉 Your pitch has been approved.\n"
                "Join our exclusive group here: https://t.me/+B-QJ-anvgPllYmRk"
            )
        else:
            update.message.reply_text(
                "Thank you for your pitch! Unfortunately, it didn’t meet our criteria this time.\n\n"
                f"Feedback:\n{evaluation}\n\n"
                "Please refine your project and try again later."
            )
    except Exception as e:
        logger.error("Error during OpenAI evaluation: %s", str(e))
        update.message.reply_text(
            "An error occurred during the evaluation. Please try again."
        )


def cancel(update, context):
    update.message.reply_text(
        "Pitching process has been cancelled. Thanks for stopping by!",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


def error_handler(update, context):
    logger.error(f"Update {update} caused error {context.error}")


def main():
    # Create an event loop
    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
    loop = asyncio.get_event_loop()

    updater = Updater(TELEGRAM_BOT_TOKEN, use_context=True)
    dispatcher = updater.dispatcher

    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(CommandHandler("help", help_command))
    dispatcher.add_handler(CommandHandler("cancel", cancel))
    dispatcher.add_handler(CommandHandler("check", check_payment))

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("pitch", pitch_start)],
        states={
            PAYMENT: [
                CommandHandler("check", check_payment),
                MessageHandler(Filters.text & ~Filters.command, receive_wallet_address),
            ],
            WAITING_FOR_WALLET_ADDRESS: [
                MessageHandler(Filters.text & ~Filters.command, receive_wallet_address)
            ],
            # The following states use the same handler for all questions
            QUESTION_1: [MessageHandler(Filters.text & ~Filters.command, handle_question)],
            QUESTION_2: [MessageHandler(Filters.text & ~Filters.command, handle_question)],
            QUESTION_3: [MessageHandler(Filters.text & ~Filters.command, handle_question)],
            QUESTION_4: [MessageHandler(Filters.text & ~Filters.command, handle_question)],
            QUESTION_5: [MessageHandler(Filters.text & ~Filters.command, handle_question)],
            QUESTION_6: [MessageHandler(Filters.text & ~Filters.command, handle_question)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    dispatcher.add_handler(conv_handler)
    dispatcher.add_error_handler(error_handler)

    # Start the bot using the event loop
    updater.start_polling()
    loop.run_forever()


if __name__ == "__main__":
    main()
