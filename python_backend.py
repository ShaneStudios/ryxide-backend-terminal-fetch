import os
import subprocess
import tempfile
import shutil
import zipfile
import logging
from flask import Flask, request, jsonify, send_file, abort, Response
from flask_cors import CORS

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

allowed_origins = "*"
CORS(app, resources={r"/fetch-and-zip": {"origins": allowed_origins}})

TEMP_DIR_BASE = None
ALLOWED_COMMANDS = {'git', 'wget'}
MAX_LOG_HEADERS = 10

def create_zip(source_dir, output_filename, logs):
    logs.append(f"Starting zip creation for '{source_dir}' -> '{output_filename}'")
    try:
        with zipfile.ZipFile(output_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(source_dir):
                rel_dir = os.path.relpath(root, source_dir)
                if rel_dir == '.':
                    rel_dir = ''

                for file in files:
                    file_path = os.path.join(root, file)
                    archive_path = os.path.join(rel_dir, file)
                    logs.append(f"Adding: {archive_path}")
                    zipf.write(file_path, archive_path)
        logs.append(f"Zip creation successful: {output_filename}")
    except Exception as e:
        logs.append(f"ERROR: Zip creation failed: {e}")
        raise e

def run_command(command_list, working_dir, logs):
    log_msg = f"Executing in {working_dir}: {' '.join(command_list)}"
    logs.append(log_msg)
    logging.info(log_msg)
    try:
        result = subprocess.run(
            command_list,
            cwd=working_dir,
            capture_output=True,
            text=True,
            check=False,
            timeout=120
        )
        logs.append(f"Command finished. Exit Code: {result.returncode}")
        if result.stdout:
            logs.append(f"Stdout captured (first 100 chars): {result.stdout[:100].strip()}...")
        if result.stderr:
            logs.append(f"Stderr captured: {result.stderr.strip()}")
        logging.info(f"Command finished. Exit Code: {result.returncode}")
        return result
    except subprocess.TimeoutExpired as e:
        log_msg = f"ERROR: Command timed out: {' '.join(command_list)}"
        logs.append(log_msg)
        logging.error(log_msg)
        raise e
    except Exception as e:
        log_msg = f"ERROR: Exception executing command: {e}"
        logs.append(log_msg)
        logging.error(log_msg, exc_info=True)
        raise e

def add_log_headers(response, logs):
    limited_logs = logs[-MAX_LOG_HEADERS:]
    for i, log_msg in enumerate(limited_logs):
        header_name = f"X-Log-Python-{i}"
        header_value = ''.join(c for c in log_msg if 31 < ord(c) < 127)
        response.headers.add('Access-Control-Expose-Headers', header_name)
        response.headers.set(header_name, header_value[:200])
    return response

@app.route('/')
# @cross_origin(origins=allowed_origins) 
# Alternative way for single routes
def index():
    return jsonify({"message": "Python File Snatcher Backend is running"})

@app.route('/fetch-and-zip', methods=['POST'])
def fetch_and_zip():
    logs = ["Python backend request received."]
    data = request.get_json()
    if not data:
        logs.append("ERROR: Invalid JSON payload received.")
        resp = jsonify({"error": "Invalid JSON payload"})
        resp.status_code = 400
        return add_log_headers(resp, logs)

    command_type = data.get('type')
    url = data.get('url')
    target_dir_name = data.get('targetDir')

    logs.append(f"Request params: type={command_type}, url={url}, targetDir={target_dir_name}")

    if not command_type or command_type not in ALLOWED_COMMANDS:
        logs.append(f"ERROR: Invalid command type '{command_type}'.")
        resp = jsonify({"error": f"Invalid or missing command type. Allowed: {', '.join(ALLOWED_COMMANDS)}"})
        resp.status_code = 400
        return add_log_headers(resp, logs)
    if not url:
        logs.append("ERROR: Missing 'url' parameter.")
        resp = jsonify({"error": "Missing 'url' parameter"})
        resp.status_code = 400
        return add_log_headers(resp, logs)

    temp_dir = None
    try:
        temp_dir = tempfile.mkdtemp(dir=TEMP_DIR_BASE)
        logs.append(f"Created temp directory: {temp_dir}")
        logging.info(f"Created temp directory: {temp_dir}")

        execution_dir = temp_dir
        content_dir = temp_dir

        if command_type == 'git':
            if not url.startswith(('http://', 'https://', 'git://')):
                logs.append(f"ERROR: Invalid git URL scheme: {url}")
                resp = jsonify({"error": "Invalid git URL scheme"})
                resp.status_code = 400
                return add_log_headers(resp, logs)

            clone_command = ['git', 'clone', '--depth=1', url]
            if target_dir_name:
                safe_target_dir = os.path.basename(target_dir_name or 'repo')
                if '..' in safe_target_dir or '/' in safe_target_dir or '\\' in safe_target_dir:
                     logs.append(f"ERROR: Invalid targetDir detected: {target_dir_name}")
                     resp = jsonify({"error": "Invalid characters in target directory"})
                     resp.status_code = 400
                     return add_log_headers(resp, logs)
                clone_target_path = os.path.join(temp_dir, safe_target_dir)
                clone_command.append(safe_target_dir)
                content_dir = clone_target_path
                logs.append(f"Cloning into specific dir: {safe_target_dir}")
            else:
                logs.append("Cloning without specific target dir.")

            result = run_command(clone_command, execution_dir, logs)

            if result.returncode != 0:
                 logs.append(f"ERROR: Git clone failed (Code: {result.returncode}). Stderr: {result.stderr.strip()}")
                 resp = jsonify({"error": f"Git clone failed. Stderr: {result.stderr.strip()}"})
                 resp.status_code = 500
                 return add_log_headers(resp, logs)

            if not target_dir_name:
                 items = [d for d in os.listdir(temp_dir) if os.path.isdir(os.path.join(temp_dir, d))]
                 if len(items) == 1:
                      content_dir = os.path.join(temp_dir, items[0])
                      logs.append(f"Determined cloned directory: {items[0]}")
                 else:
                      logs.append(f"ERROR: Could not determine cloned directory name. Found items: {items}")
                      resp = jsonify({"error": "Could not determine cloned directory name."})
                      resp.status_code = 500
                      return add_log_headers(resp, logs)

        elif command_type == 'wget':
             if not url.startswith(('http://', 'https://', 'ftp://')):
                 logs.append(f"ERROR: Invalid wget URL scheme: {url}")
                 resp = jsonify({"error": "Invalid wget URL scheme"})
                 resp.status_code = 400
                 return add_log_headers(resp, logs)

            wget_command = ['wget', '-P', temp_dir, '-nv', url]
            result = run_command(wget_command, execution_dir, logs)

            if result.returncode != 0:
                 logs.append(f"ERROR: wget failed (Code: {result.returncode}). Stderr: {result.stderr.strip()}")
                 resp = jsonify({"error": f"wget failed. Stderr: {result.stderr.strip()}"})
                 resp.status_code = 500
                 return add_log_headers(resp, logs)
        else:
             logs.append(f"ERROR: Command type '{command_type}' not implemented.")
             resp = jsonify({"error": f"Command type '{command_type}' not implemented yet."})
             resp.status_code = 400
             return add_log_headers(resp, logs)

        zip_filename = tempfile.mktemp(suffix=".zip", dir=temp_dir)
        logs.append(f"Attempting zip of '{content_dir}' -> '{zip_filename}'")

        if not os.path.exists(content_dir) or not os.listdir(content_dir):
             logs.append("Warning: Content directory is empty or missing. Creating dummy file.")
             os.makedirs(content_dir, exist_ok=True)
             open(os.path.join(content_dir, ".empty"), 'a').close()

        create_zip(content_dir, zip_filename, logs)

        logs.append(f"Sending zip file: {zip_filename}")
        logging.info(f"Sending zip file: {zip_filename}")
        response = send_file(
            zip_filename,
            mimetype='application/zip',
            as_attachment=True,
            download_name='downloaded_content.zip'
        )
        response.headers.add('Access-Control-Expose-Headers', 'Content-Disposition')
        return add_log_headers(response, logs)

    except subprocess.TimeoutExpired:
        logs.append("ERROR: Command execution timed out.")
        logging.error("Command execution timed out.")
        resp = jsonify({"error": "Command execution timed out on the server."})
        resp.status_code = 504
        return add_log_headers(resp, logs)
    except Exception as e:
        logs.append(f"ERROR: Unhandled server exception: {e}")
        logging.exception("Unhandled exception during fetch/zip process:")
        resp = jsonify({"error": f"Server error processing request: {str(e)}"})
        resp.status_code = 500
        return add_log_headers(resp, logs)
    finally:
        if temp_dir and os.path.exists(temp_dir):
            try:
                logs.append(f"Cleaning up temp directory: {temp_dir}")
                logging.info(f"Cleaning up temp directory: {temp_dir}")
                shutil.rmtree(temp_dir)
            except Exception as e:
                logs.append(f"ERROR: Failed cleaning up temp dir {temp_dir}: {e}")
                logging.error(f"Error cleaning up temp directory {temp_dir}: {e}")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8081))
    app.run(host='0.0.0.0', port=port, debug=False)
