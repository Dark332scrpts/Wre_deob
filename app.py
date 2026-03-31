from flask import Flask, request, jsonify
from flask_cors import CORS
import subprocess
import tempfile
import os
import re
import base64
import urllib.request
import time

app = Flask(__name__)
CORS(app)

# ── Fetch raw script ──────────────────────────────────────────────────────────

def fetch_script(url):
    if 'pastefy.app' in url and '/raw' not in url:
        parts = url.rstrip('/').split('/')
        pid = parts[-1] if parts[-1] else parts[-2]
        url = f"https://pastefy.app/{pid}/raw"
    elif 'pastebin.com' in url and '/raw/' not in url:
        pid = url.rstrip('/').split('/')[-1]
        url = f"https://pastebin.com/raw/{pid}"
    elif 'github.com' in url and 'raw.githubusercontent.com' not in url:
        url = url.replace('github.com', 'raw.githubusercontent.com').replace('/blob/', '/')
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read().decode('utf-8', errors='ignore'), url

# ── Lua Dumper (sandbox que intercepta loadstring) ────────────────────────────

LUA_SANDBOX = r"""
-- Sandbox WeAreDevs Dumper
-- Intercepta loadstring e captura o codigo antes de executar

local dumped_code = {}
local call_count = 0
local MAX_CALLS = 20

-- Override loadstring
local _original_load = load
local _original_loadstring = loadstring

local function safe_loadstring(code, ...)
    call_count = call_count + 1
    if type(code) == "string" and #code > 10 then
        table.insert(dumped_code, {
            index = call_count,
            code = code,
            size = #code
        })
    end
    -- nao executa, so captura
    return function() return nil end, nil
end

loadstring = safe_loadstring
load = function(code, ...)
    if type(code) == "string" then
        return safe_loadstring(code, ...)
    end
    return _original_load(code, ...)
end

-- Override funções perigosas do Roblox
local noop = function(...) return nil end
game = setmetatable({}, {
    __index = function(t, k)
        return setmetatable({}, {
            __index = function() return noop end,
            __call = noop,
            __newindex = noop
        })
    end,
    __call = noop,
    __newindex = noop
})
workspace = game
script = setmetatable({}, {__index = function() return noop end})
_G = _G or {}
shared = {}

-- HttpGet mock
game.HttpGet = function(self, url)
    return "-- mock HttpGet: " .. tostring(url)
end

-- Override require
require = function(mod)
    return setmetatable({}, {__index = function() return noop end})
end

-- Proteção contra loops infinitos
local _start = os.clock()
local _orig_pcall = pcall

-- Executa o script alvo com proteção
local ok, err = pcall(function()
    -- SCRIPT_PLACEHOLDER
end)

-- Output dos dumps
io.write("===DUMP_START===\n")
if #dumped_code == 0 then
    io.write("===NO_DUMP===\n")
else
    for i, entry in ipairs(dumped_code) do
        io.write("===CODE_" .. i .. "_START===\n")
        io.write(entry.code)
        io.write("\n===CODE_" .. i .. "_END===\n")
    end
end
io.write("===DUMP_END===\n")
if err then
    io.write("===ERROR===\n" .. tostring(err) .. "\n===ERROR_END===\n")
end
"""

def run_lua_dumper(script_content):
    # Escapa o script para inserir no sandbox
    escaped = script_content.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '')
    
    # Injeta o script dentro do sandbox via loadstring
    injected = LUA_SANDBOX.replace(
        '-- SCRIPT_PLACEHOLDER',
        f'local _target = loadstring("{escaped}") if _target then _target() end'
    )

    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.lua', delete=False, encoding='utf-8') as f:
            f.write(injected)
            tmp = f.name

        result = subprocess.run(
            ['lua', tmp],
            capture_output=True,
            text=True,
            timeout=15
        )

        output = result.stdout
        stderr = result.stderr

        # Parsear output
        dumped = []
        if '===DUMP_START===' in output:
            if '===NO_DUMP===' in output:
                dumped = []
            else:
                pattern = r'===CODE_(\d+)_START===\n(.*?)\n===CODE_\d+_END==='
                for m in re.finditer(pattern, output, re.DOTALL):
                    dumped.append({
                        'index': int(m.group(1)),
                        'code': m.group(2).strip()
                    })

        error_msg = None
        if '===ERROR===' in output:
            em = re.search(r'===ERROR===\n(.*?)\n===ERROR_END===', output, re.DOTALL)
            if em:
                error_msg = em.group(1).strip()

        return {
            'success': True,
            'dumped': dumped,
            'error': error_msg,
            'stderr': stderr[:500] if stderr else None
        }

    except subprocess.TimeoutExpired:
        return {'success': False, 'error': 'Timeout — script demorou mais de 15s', 'dumped': []}
    except FileNotFoundError:
        return {'success': False, 'error': 'Lua não encontrado no servidor', 'dumped': []}
    except Exception as e:
        return {'success': False, 'error': str(e), 'dumped': []}
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)

# ── Deobfuscator estático (fallback) ─────────────────────────────────────────

def static_deobf(script_content):
    """Tenta reverter estaticamente sem executar"""
    result_parts = []

    # 1. string.char sequences → texto
    def resolve_string_char(m):
        try:
            nums = [int(x.strip()) for x in m.group(1).split(',')]
            return '"' + ''.join(chr(n) for n in nums if 0 < n < 128) + '"'
        except:
            return m.group(0)

    code = re.sub(r'string\.char\(([0-9,\s]+)\)', resolve_string_char, script_content)

    # 2. Base64 decode em strings longas
    def try_base64(m):
        s = m.group(1)
        try:
            pad = len(s) % 4
            if pad: s += '=' * (4 - pad)
            dec = base64.b64decode(s).decode('utf-8', errors='ignore')
            if dec.isprintable() and len(dec) > 5:
                return f'"{dec}"'
        except:
            pass
        return m.group(0)

    code = re.sub(r'"([A-Za-z0-9+/]{20,}={0,2})"', try_base64, code)

    # 3. Hex literals → números
    def hex_to_num(m):
        try:
            return str(int(m.group(0), 16))
        except:
            return m.group(0)

    code = re.sub(r'\b0x[0-9A-Fa-f]+\b', hex_to_num, code)

    # 4. Remove linhas só com variáveis ofuscadas (tipo _0x, v0x)
    lines = code.split('\n')
    clean_lines = []
    for line in lines:
        stripped = line.strip()
        # mantém tudo que não é só lixo ofuscado
        if not re.match(r'^local\s+[_a-zA-Z0-9]{15,}\s*=\s*[_a-zA-Z0-9]{15,}\s*$', stripped):
            clean_lines.append(line)

    return '\n'.join(clean_lines)

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/health', methods=['GET'])
def health():
    # Verifica se Lua está instalado
    try:
        r = subprocess.run(['lua', '-v'], capture_output=True, text=True, timeout=3)
        lua_version = r.stdout.strip() or r.stderr.strip()
    except:
        lua_version = 'not found'
    return jsonify({'status': 'ok', 'lua': lua_version})

@app.route('/dump', methods=['POST'])
def dump():
    data = request.get_json()
    url = (data.get('url') or '').strip()

    if not url.startswith('http'):
        return jsonify({'error': 'URL inválida'}), 400

    try:
        script_content, raw_url = fetch_script(url)
    except Exception as e:
        return jsonify({'error': f'Erro ao buscar script: {str(e)}'}), 500

    if len(script_content) > 500_000:
        return jsonify({'error': 'Script muito grande (máx 500KB)'}), 400

    # Tenta dump via execução com sandbox
    dump_result = run_lua_dumper(script_content)

    # Fallback: deobf estático
    static_result = static_deobf(script_content)

    return jsonify({
        'raw_url': raw_url,
        'script_size': len(script_content),
        'original_script': script_content,
        'dumped_codes': dump_result.get('dumped', []),
        'dump_success': dump_result['success'] and len(dump_result.get('dumped', [])) > 0,
        'static_deobf': static_result,
        'sandbox_error': dump_result.get('error'),
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
