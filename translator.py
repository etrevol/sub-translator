import os
import sys
import time
import subprocess
from pathlib import Path
import json
import shutil
import pysrt
from dotenv import load_dotenv
from tqdm import tqdm
from google import genai

load_dotenv()

# Initialize API and model
api_key = os.environ.get("GEMINI_API_KEY")
model_name = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")

if not api_key:
    print("Error: GEMINI_API_KEY environment variable is not set. Please create a .env file with your key.", flush=True)
    sys.exit(1)

client = genai.Client(api_key=api_key)

def check_disk_space():
    # Змініть ліміт 1.0 на інше значення (у ГБ) за потреби
    min_free_gb = 1.0
    total, used, free = shutil.disk_usage(".")
    free_gb = free / (2**30)
    if free_gb < min_free_gb:
        print(f"Error: Not enough disk space. Only {free_gb:.2f} GB free, required {min_free_gb} GB.", flush=True)
        sys.exit(1)

def get_best_subtitle_stream(mkv_path):
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "s", 
        "-show_entries", "stream=index:stream_tags=language,title:stream_disposition=default", 
        "-of", "json", str(mkv_path)
    ]
    try:
        output = subprocess.check_output(cmd, text=True)
        data = json.loads(output)
        streams = data.get("streams", [])
        if not streams:
            return 0
            
        # Priority 1: English (eng or en), avoiding "signs/songs"
        for i, stream in enumerate(streams):
            tags = stream.get("tags", {})
            lang = tags.get("language", "").lower()
            title = tags.get("title", "").lower()
            if lang in ["eng", "en"] and "sign" not in title and "song" not in title:
                return i
                
        # Priority 2: Any English track (fallback)
        for i, stream in enumerate(streams):
            if stream.get("tags", {}).get("language", "").lower() in ["eng", "en"]:
                return i
                
        # Priority 3: Default disposition, avoiding "signs/songs"
        for i, stream in enumerate(streams):
            tags = stream.get("tags", {})
            title = tags.get("title", "").lower()
            if stream.get("disposition", {}).get("default", 0) == 1 and "sign" not in title and "song" not in title:
                return i
                
        # Priority 4: Default disposition (fallback)
        for i, stream in enumerate(streams):
            if stream.get("disposition", {}).get("default", 0) == 1:
                return i
                
        # Priority 3: First subtitle stream
        return 0
    except Exception:
        return 0

def extract_subtitles(mkv_path, srt_output_path):
    stream_index = get_best_subtitle_stream(mkv_path)
    cmd = [
        "ffmpeg", "-y", "-i", str(mkv_path), 
        "-map", f"0:s:{stream_index}", str(srt_output_path)
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"Extracted subtitles to {srt_output_path.name}", flush=True)

def translate_srt_file(input_srt_path, output_srt_path):
    subs = pysrt.open(str(input_srt_path))
    batch_size = 60 
    
    for i in tqdm(range(0, len(subs), batch_size), desc=f"Translating {input_srt_path.name}", unit="batch"):
        batch = subs[i:i + batch_size]
        texts_to_translate = [sub.text for sub in batch]
        combined_text = "\n|||\n".join(texts_to_translate)
        
        # Professional translation prompt with strict formatting constraints
        prompt = (
            "You are a professional movie subtitle translator. Translate the following subtitles into Ukrainian. "
            "The source language may vary, but adapt idioms, slang, and cultural references naturally while maintaining the original tone and emotion.\n"
            "CRITICAL: The text blocks are separated by the exact string '|||'. "
            "You MUST return exactly the same number of '|||' separators. "
            "Do not add any commentary or extra text.\n\n"
            f"{combined_text}"
        )
        
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt
                )
                if not response.text:
                    raise ValueError("Empty response from API (possibly blocked by safety filters).")
                    
                translated_chunks = response.text.split('|||')
                
                # Verify separator count to avoid shifting subtitle timings
                if len(translated_chunks) == len(batch):
                    for j, translated_text in enumerate(translated_chunks):
                        batch[j].text = translated_text.strip()
                    time.sleep(4) # Increased base sleep to avoid hitting 15 RPM free tier limits
                    break # Success, exit retry loop
                else:
                    print(f"\nWarning: Separator mismatch in batch {i} (attempt {attempt+1}/{max_retries}).", flush=True)
                    if attempt == max_retries - 1:
                        print(f"Failed to translate batch {i} after 3 attempts. Skipping file.", flush=True)
                        return False
                    time.sleep(4)
                        
            except Exception as e:
                if attempt < max_retries - 1 and "429" in str(e):
                    print(f"\nRate limit hit (429). Waiting 35s before retry {attempt+1}/{max_retries}...", flush=True)
                    time.sleep(35)
                else:
                    if attempt == max_retries - 1:
                        print(f"\nTranslation failed for batch {i}: {e}. Skipping file.", flush=True)
                        return False
                    time.sleep(4)
            
    subs.save(str(output_srt_path), encoding='utf-8')
    print(f"Translation complete for {output_srt_path.name}", flush=True)
    return True

def get_total_stream_count(mkv_path):
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "stream=index", 
        "-of", "csv=p=0", str(mkv_path)
    ]
    try:
        output = subprocess.check_output(cmd, text=True).strip()
        if not output: return 0
        return len(output.split('\n'))
    except Exception:
        return 0

def inject_subtitles(original_mkv, new_srt, output_mkv):
    total_streams = get_total_stream_count(original_mkv)
    cmd = [
        "ffmpeg", "-y", "-i", str(original_mkv), "-i", str(new_srt),
        "-map", "0", "-map", "1:0",
        "-c", "copy",
        f"-metadata:s:{total_streams}", "language=uk",
        f"-metadata:s:{total_streams}", "title=UA - Ukrainian",
        f"-disposition:s:{total_streams}", "default",
        str(output_mkv)
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"Created final video: {output_mkv.name}", flush=True)

def process_all_mkv_files():
    input_dir = Path("input")
    output_base = Path("output")
    mkv_files = list(input_dir.glob("*.mkv"))
    
    if not mkv_files:
        print("No .mkv files found in the 'input' directory.", flush=True)
        return
        
    check_disk_space()

    for mkv_file in mkv_files:
        # Skip files that were already processed
        if "UA-sub" in mkv_file.name:
            continue
            
        print(f"\nProcessing {mkv_file.name}...", flush=True)
        
        # Setup folder and file structure
        base_name = mkv_file.stem
        output_dir_name = f"{base_name}_UA-sub"
        output_dir = output_base / output_dir_name
        output_dir.mkdir(exist_ok=True, parents=True)
        
        original_srt = output_dir / f"{base_name}_orig.srt"
        translated_srt = output_dir / f"{base_name}_ukr.srt"
        final_mkv = output_dir / f"{base_name}_UA-sub.mkv"
        
        # Перевірка, чи файл вже був успішно оброблений
        if final_mkv.exists() and final_mkv.stat().st_size > 0:
            print(f"Вже опрацьовано, пропускаю... ({final_mkv.name})", flush=True)
            continue
        
        extract_subtitles(mkv_file, original_srt)
        
        # Check if extraction was successful before proceeding
        if original_srt.exists() and original_srt.stat().st_size > 0:
            success = translate_srt_file(original_srt, translated_srt)
            if not success:
                print(f"Skipping {mkv_file.name} due to translation errors.", flush=True)
                continue
            inject_subtitles(mkv_file, translated_srt, final_mkv)
            print(f"Success! Output saved to: {output_dir.name}", flush=True)
        else:
            print(f"Failed to extract subtitles from {mkv_file.name}. Ensure the file contains a subtitle track.", flush=True)

if __name__ == "__main__":
    process_all_mkv_files()