import pysrt
import subprocess
from deep_translator import GoogleTranslator
from tqdm import tqdm
from pathlib import Path
import json

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
    print(f"Translating {input_srt_path.name} via Google Translate (Free)...", flush=True)
    subs = pysrt.open(str(input_srt_path))
    translator = GoogleTranslator(source='auto', target='uk')
    
    # Use translate_batch for speed
    texts = [sub.text for sub in subs]
    
    chunk_size = 50
    translated_texts = []
    
    for i in tqdm(range(0, len(texts), chunk_size)):
        chunk = texts[i:i+chunk_size]
        try:
            res = translator.translate_batch(chunk)
            translated_texts.extend(res)
        except Exception as e:
            # Fallback to single translations
            for text in chunk:
                try:
                    translated_texts.append(translator.translate(text))
                except:
                    translated_texts.append(text)

    for i, sub in enumerate(subs):
        if i < len(translated_texts) and translated_texts[i]:
            sub.text = translated_texts[i]

    subs.save(str(output_srt_path), encoding='utf-8')
    print(f"Translation complete for {output_srt_path.name}", flush=True)

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

    for mkv_file in mkv_files:
        print(f"\nProcessing {mkv_file.name}...", flush=True)
        
        base_name = mkv_file.stem
        output_dir_name = f"{base_name}_UA-sub"
        output_dir = output_base / output_dir_name
        output_dir.mkdir(exist_ok=True, parents=True)
        
        original_srt = output_dir / f"{base_name}_orig.srt"
        translated_srt = output_dir / f"{base_name}_ukr.srt"
        final_mkv = output_dir / f"{base_name}_UA-sub.mkv"
        
        extract_subtitles(mkv_file, original_srt)
        
        if original_srt.exists() and original_srt.stat().st_size > 0:
            translate_srt_file(original_srt, translated_srt)
            inject_subtitles(mkv_file, translated_srt, final_mkv)
            print(f"Success! Output saved to: {output_dir}", flush=True)
        else:
            print(f"Failed to extract subtitles from {mkv_file.name}. Ensure the file contains a subtitle track.", flush=True)

if __name__ == "__main__":
    process_all_mkv_files()
