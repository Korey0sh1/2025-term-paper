from flask import Flask, request, jsonify, render_template, send_from_directory
import os
import subprocess
import uuid
from werkzeug.utils import secure_filename
from datetime import datetime
import requests
from openai import OpenAI
import json

# 全局存储API配置
api_config = {
    'url': 'your url',
    'key': 'your key',  # 示例key，实际使用时请替换
    'model': 'your model'
}

app = Flask(__name__)
app.config.update({
    'UPLOAD_FOLDER': 'uploads',
    'DECOMPILE_FOLDER': 'decompiled',
    'SECRET_KEY': 'your-secure-key-12345',
    'MAX_CONTENT_LENGTH': 500 * 1024 * 1024  # 500MB
})

# 初始化目录
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['DECOMPILE_FOLDER'], exist_ok=True)


def generate_output_filename():
    """固定输出文件名为 output.c"""
    return "output.c"


@app.route('/')
def home():
    return render_template('home.html')


@app.route('/upload', methods=['POST'])
def handle_upload():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'Empty filename'}), 400

    try:
        filename = secure_filename(file.filename)
        save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(save_path)
        return jsonify({'message': 'Upload successful'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/decompile', methods=['POST'])
def handle_decompile():
    data = request.get_json()
    if not data or 'filename' not in data:
        return jsonify({'error': 'Missing filename'}), 400

    input_filename = secure_filename(data['filename'])
    input_path = os.path.join(app.config['UPLOAD_FOLDER'], input_filename)

    if not os.path.exists(input_path):
        return jsonify({'error': 'File not found'}), 404

    try:
        # 生成唯一文件夹
        decompile_dir = str(uuid.uuid4())
        output_dir = os.path.join(app.config['DECOMPILE_FOLDER'], decompile_dir)
        os.makedirs(output_dir, exist_ok=True)

        # 设置输出路径
        output_filename = generate_output_filename()
        output_path = os.path.join(output_dir, output_filename)

        # 执行反编译
        result = subprocess.run(
            ['../toosl/RetDec-v5.0-Linux-Release/bin/retdec-decompiler', '-o', output_path, input_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120
        )

        if result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode,
                'retdec-decompiler',
                output=result.stderr
            )

        # 清理非.c文件
        for file in os.listdir(output_dir):
            file_path = os.path.join(output_dir, file)
            if os.path.isfile(file_path) and not file.endswith('.c'):
                os.remove(file_path)

        # 验证.c文件存在
        if not os.path.exists(output_path):
            return jsonify({'error': 'No .c file generated'}), 500

        # ========== 新增：执行Semgrep扫描 ==========
        semgrep_output = os.path.join(output_dir, 'semgrep_results.json')
        try:
            semgrep_result = subprocess.run(
                [
                    'semgrep',
                    '-c', './rules',
                    '--json',
                    '-o', semgrep_output,
                    output_path
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60
            )

            if semgrep_result.returncode not in [0, 1]:
                raise subprocess.CalledProcessError(
                    semgrep_result.returncode,
                    'semgrep',
                    output=semgrep_result.stderr
                )

        except subprocess.TimeoutExpired:
            return jsonify({'error': 'Semgrep扫描超时'}), 500
        except Exception as e:
            return jsonify({'error': f'Semgrep错误: {str(e)}'}), 500

        # =========================================

        return jsonify({
            'message': 'Decompilation successful',
            'output_dir': decompile_dir,
            'output_file': output_filename,
            'semgrep_report': 'semgrep_results.json'  # 新增字段
        }), 200

    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Operation timed out'}), 500
    except subprocess.CalledProcessError as e:
        return jsonify({'error': f"Decompiler error: {e.output.decode()}"}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/download/<dir_name>/<filename>')
def handle_download(dir_name, filename):
    safe_dir = secure_filename(dir_name)
    safe_filename = secure_filename(filename)
    target_dir = os.path.join(app.config['DECOMPILE_FOLDER'], safe_dir)

    if not os.path.isdir(target_dir):
        return jsonify({'error': 'Directory not found'}), 404

    try:
        return send_from_directory(
            directory=target_dir,
            path=safe_filename,
            as_attachment=True
        )
    except FileNotFoundError:
        return jsonify({'error': 'File not found'}), 404


# ========== 新增路由：获取Semgrep报告 ==========
@app.route('/semgrep/<dir_name>')
def get_semgrep_report(dir_name):
    safe_dir = secure_filename(dir_name)
    target_dir = os.path.join(app.config['DECOMPILE_FOLDER'], safe_dir)

    if not os.path.isdir(target_dir):
        return jsonify({'error': 'Directory not found'}), 404

    try:
        return send_from_directory(
            directory=target_dir,
            path='semgrep_results.json',
            as_attachment=False
        )
    except FileNotFoundError:
        return jsonify({'error': 'Semgrep报告未生成'}), 404


@app.route('/analyze/<dir_name>')
def analyze_with_ai(dir_name):

    safe_dir = secure_filename(dir_name)
    target_dir = os.path.join(app.config['DECOMPILE_FOLDER'], safe_dir)
    
    print(f"[DEBUG] 目标目录: {target_dir}")  # 检查路径是否正确
    print(f"[DEBUG] 目录是否存在: {os.path.isdir(target_dir)}")
    if not os.path.isdir(target_dir):
        return jsonify({'error': 'Directory not found'}), 404
    
    try:
        # 读取反编译代码
        code_path = os.path.join(target_dir, 'output.c')
        with open(code_path, 'r', encoding='utf-8') as f:
            code = f.read()
        
        # 读取Semgrep结果
        semgrep_path = os.path.join(target_dir, 'semgrep_results.json')

        print(f"[DEBUG] output.c 是否存在: {os.path.exists(code_path)}")
        print(f"[DEBUG] semgrep_results.json 是否存在: {os.path.exists(semgrep_path)}")
        with open(semgrep_path, 'r', encoding='utf-8') as f:
            semgrep_results = json.load(f)
            print('2')



        # 构造大模型请求数据
        prompt = f"""
        请分析以下反编译代码和Semgrep扫描结果：
        
        反编译代码：
        {code}
        
        Semgrep扫描结果：
        {json.dumps(semgrep_results, indent=2)}
        
        请：
        1. 解释代码的主要功能
        2. 分析Semgrep发现的潜在安全问题
        3. 提供改进建议
        """

        
        
        # 检查API配置
        if not api_config['url'] or not api_config['key'] or not api_config['model']:
            return jsonify({'error': '请先在源码中完整配置API URL、Key和模型'}), 400
        
        try:
            # 初始化OpenAI客户端
            client = OpenAI(
                base_url=api_config['url'],
                api_key=api_config['key'],
            )

            question = "你是一个代码安全分析助手,分析下文代码存在的漏洞并给出修改意见。按照“一、代码主要功能 二、存在漏洞 三、修复意见”三大模块为小标题输出。输出格式不要使用markdown，三个模块之间空一行句子结尾使用句号。"
            completion = client.chat.completions.create(
                model=api_config['model'],
                messages=[
                    {"role": "system", "content": question},
                    {"role": "user", "content": prompt},
                ],
            )
            # 解析大模型返回的内容
            content = completion.choices[0].message.content
            parts = content.split('、')
            code_summary = parts[1].replace('二','').strip()
            security_issues = parts[2].replace('三','').strip()
            recommendations = parts[3]

            return jsonify({
                "code_summary": code_summary,
                "security_issues": security_issues,
                "recommendations": recommendations,
            })
            
        except Exception as e:
            return jsonify({'error': f'API调用失败: {str(e)}'}), 500
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
