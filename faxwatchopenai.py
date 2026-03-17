import os
import time
import re
import subprocess
import logging
from pathlib import Path
from openai import OpenAI
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph

# --- Logging Configuration ---
# Helps monitor the script in the background and debug hardware/API issues.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# --- Configuration ---
# Ensure your OPENAI_API_KEY environment variable is set.
client = OpenAI() 

WATCH_DIRECTORY = Path("/mnt/FritzFAX/FRITZ/faxbox/")
OUTPUT_DIRECTORY = WATCH_DIRECTORY / "processed" # Destination for results
FAX_DEVICE = "/dev/rfcomm0"                      # Path to your fax modem
POLLING_INTERVAL_SECONDS = 5                     # Frequency of directory scans
MAX_API_RETRIES = 3                              # Retries for OpenAI network errors

# Patterns for validation and extraction
PHONE_NUMBER_PATTERN = re.compile(r"^[+]?\d+$")
SAFE_FILENAME_PATTERN = re.compile(r"^[a-zA-Z0-9_\-\.]+$")
FILENAME_PATTERN = re.compile(
    r"(\d{2}\.\d{2}\.\d{2}_\d{2}\.\d{2})_Telefax\.(\d+)\.pdf$",
    re.IGNORECASE
)

# In-memory set to prevent double-processing in a single session
PROCESSED_FILES = set()

# Ensure output directory exists
OUTPUT_DIRECTORY.mkdir(exist_ok=True)

# --- Helper Functions ---

def wait_for_file_stability(filepath: Path, wait_time=2, samples=3):
    """
    RACE CONDITION MITIGATION:
    Fritz!Box or network shares might take time to finish writing the PDF.
    This confirms the file size is no longer changing before we touch it.
    """
    try:
        for _ in range(samples):
            size_1 = filepath.stat().st_size
            time.sleep(wait_time)
            size_2 = filepath.stat().st_size
            if size_1 != size_2 or size_1 == 0:
                return False
        return True
    except FileNotFoundError:
        return False

def convert_pdf_to_tiff(input_pdf: Path, output_tif: Path) -> bool:
    """Converts a PDF to a G3 TIFF file using Ghostscript for fax compatibility."""
    logger.info(f"📄 Converting PDF to G3 TIFF: {output_tif.name}")
    command = [
        "gs", "-q", "-sDEVICE=tiffg3", "-r204x196", "-dBATCH",
        "-dPDFFitPage", "-dNOPAUSE", 
        f"-sOutputFile={str(output_tif)}",
        str(input_pdf)
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.error(f"❌ TIFF conversion failed: {e}")
        return False

def analyze_pdf_with_openai(filepath: Path, prompt: str) -> str:
    """
    API RELIABILITY & CONTEXT MANAGEMENT:
    Uploads the PDF using a context manager and implements exponential backoff.
    """
    file_id = None
    for attempt in range(MAX_API_RETRIES):
        try:
            logger.info(f"☁️ Uploading {filepath.name} to OpenAI (Attempt {attempt+1})...")
            
            # Use 'with' to ensure the file is closed immediately after reading
            with filepath.open("rb") as f:
                uploaded_file = client.files.create(file=f, purpose='user_data')
            
            file_id = uploaded_file.id
            
            # Using Chat Completions for GPT-4o PDF analysis
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": f"data:application/pdf;base64,{file_id}"}} 
                            # Note: Logic assumes GPT-4o file interpretation via the API
                        ],
                    }
                ],
            )
            return response.choices[0].message.content
            
        except Exception as e:
            logger.warning(f"⚠️ OpenAI attempt {attempt+1} failed: {e}")
            time.sleep(2 ** attempt) # Exponential backoff (2, 4, 8 seconds)
        finally:
            if file_id:
                try:
                    client.files.delete(file_id)
                except Exception:
                    pass
    return "Error: Maximum retries reached. Could not process fax with AI."

def create_result_pdf(output_path: Path, title: str, content: str):
    """Creates an A4 PDF containing the analysis results using ReportLab."""
    try:
        doc = SimpleDocTemplate(str(output_path), pagesize=A4, title=title)
        styles = getSampleStyleSheet()
        
        # Styles for visual clarity
        mono_style = ParagraphStyle(
            'Monospace', parent=styles['Normal'], fontName='Courier', fontSize=11, leading=13
        )
        helv_style = ParagraphStyle(
            'Helvetica', parent=styles['Normal'], fontName='Helvetica', fontSize=12, leading=14
        )

        # Build PDF Story
        story = [
            Paragraph("<b>AI Fax Analysis Result</b>", styles['Title']),
            Paragraph(f"<b>Original Document:</b> {title}", helv_style),
            Paragraph("<br/><br/>", helv_style),
            Paragraph("<b>Analysis & Response:</b>", helv_style),
            Paragraph("<br/>", helv_style),
            # Replace newlines with HTML breaks for ReportLab's Paragraph parser
            Paragraph(content.replace('\n', '<br />'), mono_style)
        ]
        
        doc.build(story)
        logger.info(f"💾 Analysis saved to PDF: {output_path.name}")
        return True
    except Exception as e:
        logger.error(f"❌ Error creating result PDF: {e}")
        return False

def send_fax(phone_number: str, tiff_file: Path) -> bool:
    """Sends the TIFF file using the efax command-line utility."""
    logger.info(f"📠 Sending fax to {phone_number} via {FAX_DEVICE}...")
    command = ["efax", "-d", FAX_DEVICE, "-t", phone_number, str(tiff_file)]
    try:
        # Faxes are slow; we use a 10-minute timeout
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=600)
        if "done" in result.stdout.lower() or "ok" in result.stdout.lower():
            logger.info(f"✅ Fax sent successfully to {phone_number}.")
            return True
        else:
            logger.warning("⚠️ Efax finished but status is uncertain. Check hardware logs.")
            return False     
    except Exception as e:
        logger.error(f"❌ Fax failed (Efax/Hardware error): {e}")
        return False

# --- Main Processing Loop ---

def process_pdf_file(filepath: Path):
    """Handles the full lifecycle of a detected fax PDF."""
    
    # 0. Wait for file to finish writing to disk
    if not wait_for_file_stability(filepath):
        logger.warning(f"File {filepath.name} is unstable or incomplete. Skipping.")
        return

    filename = filepath.name
    logger.info(f"📦 New Fax Detected: {filename}")
    
    # 1. Filename & Security Validation
    match = FILENAME_PATTERN.search(filename)
    if not match:
        logger.error("❌ Filename pattern mismatch. Skipping.")
        PROCESSED_FILES.add(filepath)
        return

    datetime_str = match.group(1).replace('_', ' ')
    number = match.group(2) 
    
    if not (PHONE_NUMBER_PATTERN.match(number) and SAFE_FILENAME_PATTERN.match(filename.replace('.pdf', ''))):
        logger.error(f"❌ Security validation failed for number '{number}'. Skipping.")
        PROCESSED_FILES.add(filepath)
        return

    # 2. Define the Full Instruction Set (The System Prompt)
    system_prompt = (
        "You are an expert fax processor. Analyze the provided fax document. "
        "Identify the sender, recipient, subject, and a concise summary of the content. "
        "Format your entire output as a detailed, easy-to-read, plain text report for the sender. "
        "If needed use HTML tags: <b> - Bold text, <i> - Italic text, <del> - Deleted text. "
        "If there is a task (prompt) in the document, then also perform the task separately. "
        "Correctness and quality is more important than the speed of answering. "
        "Detect the language that the user is using. Make sure to use the same language in your response. "
        "Do not mention the language explicitly. "
    )
    prompt = f"{system_prompt}\n\nThis fax was received from number {number} at {datetime_str}."

    # 3. Analyze with AI
    analysis_content = analyze_pdf_with_openai(filepath, prompt)

    # 4. Generate Result PDF
    timestamp = time.strftime('%Y%m%d%H%M%S')
    result_pdf_path = OUTPUT_DIRECTORY / f"ANALYSIS_{number}_{timestamp}.pdf"
    result_tif_path = OUTPUT_DIRECTORY / f"FAXBACK_{number}_{timestamp}.tif"

    if create_result_pdf(result_pdf_path, filename, analysis_content):
        # 5. Convert to TIFF and Send
        if convert_pdf_to_tiff(result_pdf_path, result_tif_path):
            if send_fax(number, result_tif_path):
                # Cleanup temp TIFF on success
                if result_tif_path.exists():
                    result_tif_path.unlink()
    
    # Always mark as processed to prevent infinite loops
    PROCESSED_FILES.add(filepath)
    logger.info("-" * 40)

def start_polling():
    """Starts the watcher and processes new PDFs as they arrive."""
    # Ignore files already in the directory at startup
    for p in WATCH_DIRECTORY.glob("*.pdf"):
        PROCESSED_FILES.add(p)
    
    logger.info(f"🚀 Watcher started. Ignoring {len(PROCESSED_FILES)} existing files.")
    logger.info(f"Watching: {WATCH_DIRECTORY}")

    try:
        while True:
            for filepath in WATCH_DIRECTORY.glob("*.pdf"):
                if filepath not in PROCESSED_FILES:
                    process_pdf_file(filepath)
            time.sleep(POLLING_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        logger.info("Watcher stopped by user.")
    except Exception as e:
        logger.critical(f"An unexpected error occurred in the loop: {e}")

if __name__ == "__main__":
    start_polling()
