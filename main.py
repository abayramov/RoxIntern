import os
import logging
import psycopg2
# from dotenv import load_dotenv
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, ConversationHandler
from telegram import ReplyKeyboardRemove
import asyncio
from openai import AsyncOpenAI

# Load environment variables
# load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
DATABASE_URL = os.getenv('DATABASE_URL')

# Instantiate OpenAI async client
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# Set up logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Database connection
conn = psycopg2.connect(DATABASE_URL)
cursor = conn.cursor()

# Create table with updated schema
create_table_query = '''
CREATE TABLE IF NOT EXISTS pitches (
    user_id BIGINT PRIMARY KEY,
    username TEXT,
    context_history TEXT,
    evaluation TEXT,
    approved BOOLEAN,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
'''
cursor.execute(create_table_query)
conn.commit()

# Define conversation states
(QUESTION_1, QUESTION_2, QUESTION_3, QUESTION_4, QUESTION_5, QUESTION_6) = range(6)

# Updated list of questions
questions = [
    "What problem does your project aim to solve?",
    "Who is your target audience? How will you acquire them?",
    "What is your unique value proposition?",
    "Do you have a prototype or MVP? If so, do you have any traction?",
    "Tell me about your team and their backgrounds. Have you worked together before?",
    "How much are you looking to raise? Do you have a lead investor?"
]

def start(update, context):
    welcome_message = (
        "Hey, I'm Analyst AI - the chief of all analysts at AnalystDAO 🤓\n\n"
        "I'll ask a series of questions to learn more about your project... \n\n"
        "If I like it, you you'll be invited to an exclusive TG group & get the chance to pitch to top VC investors \n\n"
        "Ready to dive in? Type /pitch to get started."
    )
    update.message.reply_text(welcome_message)

def help_command(update, context):
    help_text = (
        "Available commands:\n"
        "/start - Welcome message\n"
        "/pitch - Begin pitching your project\n"
        "/help - Show this help message\n"
        "/cancel - Cancel the pitching process"
    )
    update.message.reply_text(help_text)

def pitch_start(update, context):
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.full_name

    # Initialize user data
    context.user_data.clear()
    context.user_data['answers'] = []
    context.user_data['user_id'] = user_id
    context.user_data['username'] = username
    context.user_data['context_history'] = ""

    update.message.reply_text(
        "Alright, let's go! 💡\n" + questions[0]
    )
    return QUESTION_1

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

async def handle_question_async(update, context):
    user_data = context.user_data
    answers = user_data['answers']
    context_history = user_data.setdefault('context_history', "")

    # Append the user's answer
    user_response = update.message.text
    answers.append(user_response)
    context_history += f"Question {len(answers)}: {questions[len(answers) - 1]}\n"
    context_history += f"Answer: {user_response}\n"

    # Save updated context_history
    user_data['context_history'] = context_history

    # Prepare AI prompt
    messages = [
        {
            "role": "system",
            "content": (
                "You are a sarcastic, witty VC intern evaluating a startup pitch. "
                "You're playful, but also cynical like Simon Cowell. "
                "Avoid repeating similar feedback or jokes. "
                "Keep track of the conversation so far and ensure your responses are varied."
            )
        }
    ]

    # Build the conversation history
    exchanges = context_history.strip().split('\n')
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
        messages.append({
            "role": role,
            "content": content
        })

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
        user_data['context_history'] = context_history

        update.message.reply_text(ai_reply)

        # Move to the next question
        if len(answers) < len(questions):
            update.message.reply_text(questions[len(answers)])
            return len(answers)
        else:
            update.message.reply_text("That’s it for the questions! Let me process your responses...")
            await evaluate_pitch(update, context)
            return ConversationHandler.END

    except Exception as e:
        logger.error("Error during OpenAI API call: %s", str(e))
        update.message.reply_text("An error occurred while communicating with the AI. Please try again.")
        return ConversationHandler.END

async def evaluate_pitch(update, context):
    user_data = context.user_data
    context_history = user_data.get('context_history', "")

    if not context_history.strip():
        update.message.reply_text("It seems there was no context to evaluate. Please try again.")
        return

    messages = [
        {
            "role": "system",
            "content": (
                "You are a VC intern tasked with evaluating startup pitches. "
                "Based on the following conversation, provide an overall evaluation. "
                "Include a decision ('Approved' or 'Not Approved') and a brief explanation."
            )
        }
    ]

    # Build the conversation history
    exchanges = context_history.strip().split('\n')
    for exchange in exchanges:
        if exchange.startswith("AI Response"):
            role = "assistant"
            content = exchange.replace("AI Response:", "").strip()
        else:
            role = "user"
            content = exchange.split(":", 1)[1].strip() if ':' in exchange else exchange
        messages.append({
            "role": role,
            "content": content
        })

    logger.info("Prepared evaluation messages for OpenAI API: %s", messages)

    try:
        response = await openai_client.chat.completions.create(
            model="gpt-4",
            messages=messages,
            temperature=0.7,
        )

        evaluation = response.choices[0].message.content.strip()
        approved = "Approved" in evaluation and "Not Approved" not in evaluation  # Updated logic

        # Save to database
        cursor.execute('''
            INSERT INTO pitches (user_id, username, context_history, evaluation, approved)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
            context_history = EXCLUDED.context_history,
            evaluation = EXCLUDED.evaluation,
            approved = EXCLUDED.approved,
            timestamp = CURRENT_TIMESTAMP
        ''', (
            user_data['user_id'],
            user_data['username'],
            context_history,
            evaluation,
            approved
        ))
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
        update.message.reply_text("An error occurred during the evaluation. Please try again.")

def cancel(update, context):
    update.message.reply_text(
        'Pitching process has been cancelled. Thanks for stopping by!',
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END

def error_handler(update, context):
    logger.error(f"Update {update} caused error {context.error}")

def main():
    updater = Updater(TELEGRAM_BOT_TOKEN, use_context=True)
    dispatcher = updater.dispatcher

    dispatcher.add_handler(CommandHandler('start', start))
    dispatcher.add_handler(CommandHandler('help', help_command))

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('pitch', pitch_start)],
        states={
            QUESTION_1: [MessageHandler(Filters.text & ~Filters.command, handle_question)],
            QUESTION_2: [MessageHandler(Filters.text & ~Filters.command, handle_question)],
            QUESTION_3: [MessageHandler(Filters.text & ~Filters.command, handle_question)],
            QUESTION_4: [MessageHandler(Filters.text & ~Filters.command, handle_question)],
            QUESTION_5: [MessageHandler(Filters.text & ~Filters.command, handle_question)],
            QUESTION_6: [MessageHandler(Filters.text & ~Filters.command, handle_question)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        allow_reentry=True
    )

    dispatcher.add_handler(conv_handler)
    dispatcher.add_error_handler(error_handler)

    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
