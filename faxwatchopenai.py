import os
import time
import re
import subprocess
from pathlib import Path
from openai import OpenAI
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter, A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph

# --- Configuration ---
# Set your OpenAI API Key from an environment variable for security
client = OpenAI() 

WATCH_DIRECTORY = Path("/mnt/FritzFAX/FRITZ/faxbox/")
OUTPUT_DIRECTORY = WATCH_DIRECTORY / "processed" # Directory for result PDFs and TIFFs
FAX_DEVICE = "/dev/rfcomm0" # Your configured fax device for efax
POLLING_INTERVAL_SECONDS = 5 # How often to check the directory (e.g., every 5 seconds)

# Regex to match the filename pattern and capture groups
FILENAME_PATTERN = re.compile(
    r"(\d{2}\.\d{2}\.\d{2}_\d{2}\.\d{2})_Telefax\.(\d+)\.pdf$",
    re.IGNORECASE
)

# Set to store paths of files that have been successfully processed
PROCESSED_FILES = set()

# --- Ensure output directory exists ---
OUTPUT_DIRECTORY.mkdir(exist_ok=True)
print(f"📁 Watching: {WATCH_DIRECTORY}")
print(f"📁 Results in: {OUTPUT_DIRECTORY}")
print(f"⏰ Polling interval: {POLLING_INTERVAL_SECONDS} seconds")
# ---------------------

# --- Fax Processing Functions ---

def convert_pdf_to_tiff(input_pdf: Path, output_tif: Path) -> bool:
    """Converts a PDF to a G3 TIFF file using Ghostscript (gs) for fax compatibility."""
    print(f"📄 Converting PDF to G3 TIFF for faxing...")
    
    command = [
        "gs", "-q", "-sDEVICE=tiffg3", "-r204x196", "-dBATCH",
        "-dPDFFitPage", "-dNOPAUSE", 
        f"-sOutputFile={str(output_tif)}",
        str(input_pdf)
    ]
    
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
        print(f"✅ TIFF conversion successful: {output_tif.name}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ TIFF conversion failed (Ghostscript error): {e.stderr}")
        return False
    except FileNotFoundError:
        print("❌ Error: 'gs' (Ghostscript) command not found. Please ensure it's installed.")
        return False

def send_fax(phone_number: str, tiff_file: Path) -> bool:
    """Sends the TIFF file as a fax using efax."""
    print(f"📠 Sending fax to {phone_number} from device {FAX_DEVICE}...")
    
    command = [
        "efax", "-d", FAX_DEVICE, "-t", phone_number, str(tiff_file)
    ]
    
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=600) 
        
        if "done" in result.stdout.lower() or "ok" in result.stdout.lower():
            print(f"✅ Fax sent successfully to {phone_number}.")
            return True
        else:
            print(f"⚠️ Efax completed, but status is uncertain. Check logs.")
            return False
            
    except subprocess.CalledProcessError as e:
        print(f"❌ Fax failed (Efax error): {e.stderr}")
        return False
    except FileNotFoundError:
        print("❌ Error: 'efax' command not found. Please ensure it's installed and configured.")
        return False
    except subprocess.TimeoutExpired:
        print("❌ Fax attempt timed out (600 seconds). Check modem connection.")
        return False

# --- PDF Creation Function ---

def create_result_pdf(output_path: Path, title: str, content: str):
    """
    Creates a new PDF file (A4, Monospace font) containing the analysis result.
    """
    try:
        doc = SimpleDocTemplate(
            str(output_path),
            pagesize=A4,
            title=title
        )
        styles = getSampleStyleSheet()
        story = []
        # Define a Monospace Style (using Courier, a standard PDF font)
        monospace_style = ParagraphStyle(
            name='Monospace',
            parent=styles['Normal'],
            fontName='Courier',
            fontSize=12,
            leading=14,
            spaceAfter=10,
        )
        # Title and Metadata
        story.append(Paragraph(f"<b>OpenAI Fax Analysis Result</b>", styles['Title']))
        story.append(Paragraph(f"Document: {title}", styles['Normal']))
        story.append(Paragraph("<br/>", styles['Normal']))
        # Content
        # Analysis content uses the Monospace style
        story.append(Paragraph(f"<b>Analysis:</b>", styles['Normal']))
        # We use a single Paragraph with the Monospace style for the content block.
        # Note: We replace newlines with line breaks (<br/>) for ReportLab
        formatted_content = content.replace('\n', '<br/>')
        story.append(Paragraph(formatted_content, monospace_style))
        doc.build(story)
        print(f"💾 Analysis saved to PDF (A4, Monospace): {output_path.name}")
        return True
    except Exception as e:
        print(f"Error creating result PDF: {e}")
        return False

# --- OpenAI Function ---

def analyze_pdf_with_openai(filepath: Path, prompt: str) -> str:
    """Uploads the PDF to OpenAI, runs an analysis using gpt-4o, and returns the response."""
    print(f"☁️ Uploading file to OpenAI: {filepath.name}...")
    file_id = None
    try:
        uploaded_file = client.files.create(file=open(filepath, "rb"), purpose='user_data')
        file_id = uploaded_file.id
        print(f"   -> File uploaded successfully. File ID: {file_id}")

        response = client.responses.create(
            model="gpt-4o",
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_file", "file_id": file_id}
                    ],
                }
            ],
        )
        analysis_result = response.output_text
        print(f"   -> Analysis complete.")
        return analysis_result

    except Exception as e:
        print(f"🔴 OpenAI API Error during processing: {e}")
        return f"Error processing file with OpenAI: {e}"

    finally:
        if file_id:
            try:
                client.files.delete(file_id)
                print(f"   -> Cleaned up uploaded file: {file_id}")
            except Exception as e:
                print(f"Warning: Could not delete file {file_id} from OpenAI: {e}")

# --- Polling Logic ---

def process_pdf_file(filepath: Path):
    """
    Handles the core logic for a single detected PDF file.
    """
    filename = filepath.name
    print("\n" + "=" * 80)
    print(f"📦 New Fax File Detected: {filename}")

    match = FILENAME_PATTERN.search(filename)
    
    if not match:
        print(f"❌ Filename does not match the required pattern. Skipping.")
        PROCESSED_FILES.add(filepath) # Add to set to avoid reprocessing
        print("=" * 80)
        return
        
    datetime_str = match.group(1).replace('_', ' ')
    number = match.group(2) # The number to fax back to

    print(f"✅ Extracted Datetime: {datetime_str} | Fax-Back Number: {number}")
    
    # --- 1. Define the Prompt for OpenAI ---
    system_prompt = (
        "You are an expert fax processor. Analyze the provided fax document. "
        "Identify the sender, recipient, subject, and a concise summary of the content. "
        "Format your entire output as a detailed, easy-to-read, plain text report for the sender. "
        "If there is a task (prompt) in the document, then also perform the task separately. "
    )
    prompt = f"{system_prompt}\n\nThis fax was received from number {number} at {datetime_str}."

    # --- 2. Send to OpenAI for Analysis ---
    analysis_content = analyze_pdf_with_openai(filepath, prompt)
    
    # --- 3. Save the Result as PDF ---
    base_filename = filename.replace('.pdf', '')
    timestamp = time.strftime('%Y%m%d%H%M%S')
    
    result_pdf_filename = f"ANALYSIS_{base_filename}_{timestamp}.pdf"
    result_pdf_filepath = OUTPUT_DIRECTORY / result_pdf_filename

    if not create_result_pdf(result_pdf_filepath, filename, analysis_content):
        print("⚠️ Skipping fax-back due to failure in creating result PDF.")
        PROCESSED_FILES.add(filepath)
        print("=" * 80)
        return

    # --- 4. Convert Result PDF to TIFF ---
    result_tif_filename = f"FAXBACK_{base_filename}_{timestamp}.tif"
    result_tif_filepath = OUTPUT_DIRECTORY / result_tif_filename
    
    if not convert_pdf_to_tiff(result_pdf_filepath, result_tif_filepath):
        print("⚠️ Skipping fax-back due to failure in TIFF conversion.")
        PROCESSED_FILES.add(filepath)
        print("=" * 80)
        return

    # --- 5. Send the Fax Back ---
    send_fax(number, result_tif_filepath)
    
    # --- 6. Cleanup & Mark as Processed ---
    try:
        os.remove(result_tif_filepath)
        print(f"🧹 Cleaned up temporary TIFF file: {result_tif_filepath.name}")
    except OSError as e:
        print(f"Warning: Failed to remove temporary TIFF file: {e}")
    
    # IMPORTANT: Mark the original file as processed so it's ignored on the next poll
    PROCESSED_FILES.add(filepath)
    
    print("=" * 80)

def start_polling():
    """
    Initializes and runs the polling loop to check the directory.
    """
    # 1. On startup, populate the processed set with all existing files
    # to prevent immediate processing of old files.
    for p in WATCH_DIRECTORY.glob("*.pdf"):
        PROCESSED_FILES.add(p)
    
    print(f"✅ Initial scan complete. {len(PROCESSED_FILES)} existing PDFs found and marked as processed.")
    print("Waiting for new PDF files...")

    try:
        while True:
            # 2. Scan for new files
            for filepath in WATCH_DIRECTORY.glob("*.pdf"):
                if filepath not in PROCESSED_FILES:
                    # A new, unprocessed file has been found
                    process_pdf_file(filepath)
            
            # 3. Wait for the next poll
            time.sleep(POLLING_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        print("\nWatcher stopped by user.")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        
if __name__ == "__main__":
    start_polling()
