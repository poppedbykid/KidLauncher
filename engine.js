const { Client } = require('minecraft-launcher-core');
const path = require('path');
const readline = require('readline');
const fs = require('fs');
const axios = require('axios');
const { execSync, spawnSync } = require('child_process');

const launcher = new Client();
const rl = readline.createInterface({ input: process.stdin, output: process.stdout, terminal: false });

function log(msg)      { process.stdout.write(JSON.stringify({ type: 'log', message: msg }) + '\n'); }
function progress(p)   { process.stdout.write(JSON.stringify({ type: 'progress', data: p }) + '\n'); }
function sendMods(inst, mods) { process.stdout.write(JSON.stringify({ type: 'mod_list', instance: inst, mods }) + '\n'); }

// ─────────────────────────────────────────────────────────────────────────────
// JAVA DETECTION
// ─────────────────────────────────────────────────────────────────────────────
function getJavaPath(version) {
    // For MC 1.16 and below use Java 8, 1.17 needs Java 16+, 1.18+ needs Java 17+
    const javaHomes = [
        process.env.JAVA_HOME,
        'C:\\Program Files\\Java\\jdk-21\\bin\\java.exe',
        'C:\\Program Files\\Java\\jdk-17\\bin\\java.exe',
        'C:\\Program Files\\Java\\jdk-16\\bin\\java.exe',
        'C:\\Program Files\\Java\\jre-1.8\\bin\\java.exe',
        'C:\\Program Files\\Java\\jdk1.8.0_341\\bin\\java.exe',
        'C:\\Program Files\\Eclipse Adoptium\\jdk-21.0.3.9-hotspot\\bin\\java.exe',
        'C:\\Program Files\\Eclipse Adoptium\\jdk-17.0.11.9-hotspot\\bin\\java.exe',
        'C:\\Program Files\\Microsoft\\jdk-21.0.3.9-hotspot\\bin\\java.exe',
        'C:\\Program Files\\Microsoft\\jdk-17.0.11.9-hotspot\\bin\\java.exe',
        'C:\\Program Files\\Common Files\\Oracle\\Java\\javapath\\java.exe',
    ].filter(Boolean);

    for (const p of javaHomes) {
        try { if (p && fs.existsSync(p)) return p; } catch(_) {}
    }
    return 'java'; // fallback to PATH
}

// ─────────────────────────────────────────────────────────────────────────────
// PATHS
// ─────────────────────────────────────────────────────────────────────────────
const BASE_DIR    = path.resolve(process.cwd());
const SHARED_ROOT = path.join(BASE_DIR, '.minecraft');

// ─────────────────────────────────────────────────────────────────────────────
// HIGH-PERFORMANCE JVM ARGUMENTS (tuned for Minecraft FPS recovery)
// ─────────────────────────────────────────────────────────────────────────────
function buildJvmArgs(instanceDir, ramGb) {
    const ram = typeof ramGb === 'string' ? parseInt(ramGb.replace(/[^0-9]/g, '')) : ramGb;
    const heapMb = (ram || 4) * 1024;
    const newGenMb = Math.floor(heapMb * 0.3);      // Young gen = 30% of heap
    const survivorRatio = 6;

    return [
        // ── Garbage Collection (G1GC tuned for low latency / high throughput) ──
        '-XX:+UseG1GC',
        '-XX:+ParallelRefProcEnabled',
        '-XX:MaxGCPauseMillis=200',
        '-XX:+UnlockExperimentalVMOptions',
        '-XX:+DisableExplicitGC',
        `-XX:G1NewSizePercent=30`,
        `-XX:G1MaxNewSizePercent=40`,
        `-XX:G1HeapRegionSize=8M`,
        `-XX:G1ReservePercent=20`,
        `-XX:G1HeapWastePercent=5`,
        `-XX:G1MixedGCCountTarget=4`,
        `-XX:InitiatingHeapOccupancyPercent=15`,
        `-XX:G1MixedGCLiveThresholdPercent=90`,
        `-XX:G1RSetUpdatingPauseTimePercent=5`,
        `-XX:SurvivorRatio=${survivorRatio}`,
        '-XX:+PerfDisableSharedMem',         // Reduce JVM overhead
        '-XX:MaxTenuringThreshold=1',
        // ── JIT Compiler Optimizations ──
        '-XX:+OptimizeStringConcat',
        '-XX:+UseStringDeduplication',
        '-XX:+UseCompressedOops',
        '-server',
        '-XX:+UseFastAccessorMethods',
        '-XX:+UseVectorizedMismatchIntrinsic',
        '-XX:MaxInlineLevel=15',
        '-XX:+UseFPUForSpilling',
        '-XX:+UseCriticalJavaPriority',
        // ── System tuning ──
        '-Dsun.java2d.opengl=true',
        '-Dsun.java2d.d3d=false',
        '-Dsun.java2d.noddraw=true',
        '-Djava.net.preferIPv4Stack=true',
        // ── Fabric mods directory override ──
        `-Dfabric.mods.directory=${path.join(instanceDir, 'mods')}`,
    ];
}


// ─────────────────────────────────────────────────────────────────────────────
// FABRIC INSTALLER (cached)
// ─────────────────────────────────────────────────────────────────────────────
async function installFabric(mcVersion, loaderVersion = '0.15.11') {
    const installerUrl = `https://maven.fabricmc.net/net/fabricmc/fabric-installer/1.0.1/fabric-installer-1.0.1.jar`;
    const installerPath = path.join(BASE_DIR, 'fabric-installer.jar');

    if (!fs.existsSync(installerPath)) {
        log('[FABRIC] Downloading installer...');
        const res = await axios({ url: installerUrl, method: 'GET', responseType: 'arraybuffer' });
        fs.writeFileSync(installerPath, Buffer.from(res.data));
    }

    const jPath = getJavaPath(mcVersion);
    log(`[FABRIC] Installing fabric-loader-${loaderVersion} for MC ${mcVersion}...`);
    execSync(`"${jPath}" -jar "${installerPath}" client -dir "${SHARED_ROOT}" -mcversion ${mcVersion} -loader ${loaderVersion} -noprofile`);
    return `fabric-loader-${loaderVersion}-${mcVersion}`;
}

// ─────────────────────────────────────────────────────────────────────────────
// QUILT INSTALLER
// ─────────────────────────────────────────────────────────────────────────────
async function installQuilt(mcVersion) {
    const installerUrl = 'https://quiltmc.org/api/v1/download-latest-installer/java-universal';
    const installerPath = path.join(BASE_DIR, 'quilt-installer.jar');

    if (!fs.existsSync(installerPath)) {
        log('[QUILT] Downloading installer...');
        const res = await axios({ url: installerUrl, method: 'GET', responseType: 'arraybuffer', maxRedirects: 10 });
        fs.writeFileSync(installerPath, Buffer.from(res.data));
    }
    const jPath = getJavaPath(mcVersion);
    log(`[QUILT] Installing Quilt for MC ${mcVersion}...`);
    execSync(`"${jPath}" -jar "${installerPath}" install client ${mcVersion} --install-dir="${SHARED_ROOT}" --no-profile`);

    // Find the created version ID
    const versionsDir = path.join(SHARED_ROOT, 'versions');
    const quiltVer = fs.readdirSync(versionsDir).find(v => v.startsWith('quilt-loader') && v.includes(mcVersion));
    return quiltVer || `quilt-loader-${mcVersion}`;
}

// ─────────────────────────────────────────────────────────────────────────────
// MODRINTH – resolve best file for MC version + loader
// ─────────────────────────────────────────────────────────────────────────────
async function resolveModrinthFile(projectId, mcVersion, loader) {
    const res = await axios.get(`https://api.modrinth.com/v2/project/${projectId}/version`);
    const versions = res.data;

    // Try exact match first
    let match = versions.find(v =>
        v.game_versions.includes(mcVersion) &&
        v.loaders.map(l => l.toLowerCase()).includes(loader.toLowerCase())
    );

    // Fallback: same MC version, any loader
    if (!match) match = versions.find(v => v.game_versions.includes(mcVersion));

    // Fallback: just latest
    if (!match) match = versions[0];

    if (!match || !match.files || match.files.length === 0) throw new Error('No compatible file found on Modrinth');
    return match.files[0];
}

// ─────────────────────────────────────────────────────────────────────────────
// HELPERS
// ─────────────────────────────────────────────────────────────────────────────
function getModsDir(instance) {
    return path.join(SHARED_ROOT, 'instances', instance, 'mods');
}

function listMods(instance) {
    const dir = getModsDir(instance);
    fs.mkdirSync(dir, { recursive: true });
    return fs.readdirSync(dir).filter(f => f.endsWith('.jar') || f.endsWith('.jar.disabled'));
}

// ─────────────────────────────────────────────────────────────────────────────
// MAIN COMMAND HANDLER
// ─────────────────────────────────────────────────────────────────────────────
rl.on('line', async (line) => {
    let data;
    try { data = JSON.parse(line); } catch(_) { return; }

    try {
        // ── LAUNCH ──────────────────────────────────────────────────────────
        if (data.action === 'launch') {
            const instanceDir = data.instance
                ? path.join(SHARED_ROOT, 'instances', data.instance)
                : SHARED_ROOT;
            fs.mkdirSync(instanceDir, { recursive: true });
            fs.mkdirSync(path.join(instanceDir, 'mods'), { recursive: true });

            const mcVersion  = data.version  || '1.20.1';
            const ram        = data.ram       || '4G';
            const loader     = (data.loader   || 'VANILLA').toUpperCase();
            const javaPath   = getJavaPath(mcVersion);

            log(`[SOLAR] Preparing ${loader} ${mcVersion} — RAM: ${ram}`);

            let targetVersion = mcVersion;

            if (loader === 'FABRIC') {
                targetVersion = await installFabric(mcVersion);
                log(`[FABRIC] Target locked: ${targetVersion}`);
            } else if (loader === 'QUILT') {
                targetVersion = await installQuilt(mcVersion);
                log(`[QUILT] Target locked: ${targetVersion}`);
            }

            const jvmArgs = buildJvmArgs(instanceDir, ram);

            const opts = {
                authorization: {
                    access_token: data.accessToken || 'null',
                    client_token: data.clientToken || 'null',
                    uuid: data.uuid || '00000000-0000-0000-0000-000000000000',
                    name: data.username || 'Player',
                },
                root: SHARED_ROOT,
                version: { number: targetVersion, type: 'release' },
                memory: { max: ram, min: ram },
                javaPath,
                jvmOptions: jvmArgs,
                overrides: { gameDirectory: instanceDir },
                skipAssets: false,
            };

            log('[SOLAR] NUCLEAR IGNITION — launching...');
            progress({ type: 'ignition', current: 100, total: 100, step: 'GAME STARTING' });

            launcher.launch(opts);

            launcher.on('data', (e) => log(`[MC] ${e}`));
            launcher.on('progress', (e) => {
                progress({ type: e.type, current: e.task, total: e.total, step: e.type });
            });
            launcher.on('close', (code) => log(`[MC] Process exited: ${code}`));
        }

        // ── LIST MODS ────────────────────────────────────────────────────────
        else if (data.action === 'list_mods') {
            sendMods(data.instance, listMods(data.instance));
        }

        // ── TOGGLE MOD (enable/disable) ──────────────────────────────────────
        else if (data.action === 'toggle_mod') {
            const dir  = getModsDir(data.instance);
            const full = path.join(dir, data.filename);
            if (data.filename.endsWith('.disabled')) {
                fs.renameSync(full, full.slice(0, -9)); // remove .disabled
            } else {
                fs.renameSync(full, full + '.disabled');
            }
            sendMods(data.instance, listMods(data.instance));
        }

        // ── REMOVE MOD ───────────────────────────────────────────────────────
        else if (data.action === 'remove_mod') {
            const target = path.join(getModsDir(data.instance), data.filename);
            if (fs.existsSync(target)) fs.unlinkSync(target);
            sendMods(data.instance, listMods(data.instance));
        }

        // ── INJECT MOD (drag & drop base64) ─────────────────────────────────
        else if (data.action === 'inject_mod') {
            const dir = getModsDir(data.instance);
            const buf = Buffer.from(data.data, 'base64');
            fs.writeFileSync(path.join(dir, data.filename), buf);
            log(`[MOD] Injected: ${data.filename}`);
            sendMods(data.instance, listMods(data.instance));
        }

        // ── INSTALL MOD FROM MODRINTH ────────────────────────────────────────
        else if (data.action === 'install_mod') {
            const { instance, modId, mcVersion, loader } = data;
            const dir = getModsDir(instance);

            log(`[MODRINTH] Resolving ${modId} for MC ${mcVersion} + ${loader}...`);
            const file = await resolveModrinthFile(modId, mcVersion || '1.20.1', loader || 'fabric');

            log(`[MODRINTH] Downloading: ${file.filename}`);
            const response = await axios({
                url: file.url,
                method: 'GET',
                responseType: 'arraybuffer',
                onDownloadProgress: (pe) => {
                    progress({ type: 'download-status', name: file.filename, current: pe.loaded, total: pe.total || pe.loaded });
                },
            });

            fs.writeFileSync(path.join(dir, file.filename), Buffer.from(response.data));
            progress({ type: 'download-finished' });
            log(`[MODRINTH] Installed: ${file.filename}`);
            sendMods(instance, listMods(instance));
        }

        // ── ADD INSTANCE (create folder structure) ───────────────────────────
        else if (data.action === 'add_instance') {
            const instDir  = path.join(SHARED_ROOT, 'instances', data.name);
            const modsDir  = path.join(instDir, 'mods');
            const savesDir = path.join(instDir, 'saves');
            fs.mkdirSync(modsDir,  { recursive: true });
            fs.mkdirSync(savesDir, { recursive: true });
            log(`[INSTANCE] Created: ${data.name} (${data.loader} ${data.version})`);
        }

    } catch (e) {
        log(`[FATAL] ${e.message}`);
    }
});

log('[SOLAR] ⚡ High-Performance Engine v3.0 Active — JVM tuned for max FPS.');
