# Automated MKV Subtitle Translator

Automated Python tool designed to extract English subtitles from MKV video files, translate them to Ukrainian, and losslessly inject them back as a new subtitle track.

*Vibecoded by Artem etrevol Holovashchenko.*

## Features
- **Batch Processing:** Automatically processes all MKV files placed in the `input` directory.
- **Two Translation Modes:**
  - **Gemini API (`translator.py`)**: High-quality contextual translation using Google's generative models (`gemini-3.5-flash-lite`).
  - **Google Translate (`Google_Translate_Engine_translator.py`)**: Fast, completely free translation with zero API limits using `deep-translator`.
- **Lossless Subtitle Injection**: Retains all original video, audio, and subtitle streams. Adds the new Ukrainian subtitle track seamlessly and sets it as the default.

## Prerequisites
- **Python 3.8+**
- **FFmpeg**: Must be installed and accessible in your system's PATH.
- **Python Packages**: Install required dependencies using:

    ```bash
    pip install -r requirements.txt
    ```

## Setup & Usage

### 1. (Optional) Gemini API Setup
If you want to use the high-quality translation script (`translator.py`), you need a Gemini API key.
Create a `.env` file in the root directory and add your key:
```env
GEMINI_API_KEY=your_api_key_here
GEMINI_MODEL=gemini-3.5-flash-lite
```
*(You can change `GEMINI_MODEL` to any other available Gemini model you prefer).*

### 2. Execution
1. Place your `.mkv` movie file(s) into the `input/` folder.
2. Run your preferred script:
   - For Gemini API: `python translator.py`
   - For free Google Translate: `python Google_Translate_Engine_translator.py`
3. Retrieve your processed MKV files from the `output/` folder.

## Project Structure
- `input/`: Place your original `.mkv` files here.
- `output/`: The script will create folders here for each processed movie, containing the extracted English subtitles, the translated Ukrainian subtitles, and the final combined MKV file.
- `translator.py`: Script utilizing the Gemini API for contextual translation.
- `Google_Translate_Engine_translator.py`: Fallback script utilizing free Google Translate.
