import { createHash } from 'node:crypto';
import { mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const upstream = 'https://raw.githubusercontent.com/SpeedeMiner/RU-Speede-VPN/main/happ/ru-speede-vpn.json';
const sourceBase = 'https://cdn.jsdelivr.net/gh/SpeedeMiner/Geosite-ru-light@release/geosite_RU.dat';
const publicBase = 'https://raw.githubusercontent.com/Menshikovivan/FargoVPN/main/routing/public';
const exception = 'proxy.mob.maps.yandex.net';
const output = fileURLToPath(new URL('./public/', import.meta.url));
const productionName = 'RU-Speede-VPN';
const testName = 'FargoVPN Navigator Test';

function sha256(bytes) {
  return createHash('sha256').update(bytes).digest('hex');
}

async function download(url, maxBytes) {
  const response = await fetch(url, { redirect: 'error', signal: AbortSignal.timeout(25000) });
  if (!response.ok || !response.body) throw new Error('Download failed with HTTP ' + response.status);
  const chunks = [];
  let size = 0;
  for await (const chunk of response.body) {
    size += chunk.length;
    if (size > maxBytes) throw new Error('Upstream file exceeds size limit');
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

function* protobufFields(bytes) {
  let offset = 0;
  function uint() {
    let value = 0;
    for (let shift = 0; offset < bytes.length && shift < 49; shift += 7) {
      const octet = bytes[offset++];
      value += (octet & 127) * 2 ** shift;
      if ((octet & 128) === 0) return value;
    }
    throw new Error('Invalid protobuf varint');
  }
  while (offset < bytes.length) {
    const start = offset;
    const key = uint();
    const type = key % 8;
    const field = Math.floor(key / 8);
    let value;
    if (!field) throw new Error('Invalid protobuf field');
    if (type === 0) value = uint();
    else if (type === 2) {
      const length = uint();
      value = bytes.subarray(offset, offset + length);
      offset += length;
    } else if (type === 1 || type === 5) offset += type === 1 ? 8 : 4;
    else throw new Error('Unsupported protobuf wire type');
    if (offset > bytes.length) throw new Error('Truncated protobuf field');
    yield { field, value, raw: bytes.subarray(start, offset) };
  }
}

function encodeLength(length) {
  const octets = [];
  while (length >= 128) {
    octets.push((length & 127) | 128);
    length = Math.floor(length / 128);
  }
  octets.push(length);
  return Buffer.from(octets);
}

function patchGeosite(data) {
  let blockCategories = 0;
  let removed = 0;
  const categories = new Map();
  const outputFields = [...protobufFields(data)].map(entry => {
    if (entry.field !== 1 || !Buffer.isBuffer(entry.value)) return entry.raw;
    const fields = [...protobufFields(entry.value)];
    const name = fields.find(field => field.field === 1)?.value?.toString().toUpperCase();
    if (!name || categories.has(name)) throw new Error('Invalid GeoSite category');
    const rules = fields.filter(field => field.field === 2);
    function unpack(rule) {
      const f = new Map([...protobufFields(rule.value)].map(({ field, value }) => [field, value]));
      return { type: f.get(1) ?? 0, value: f.get(2)?.toString().toLowerCase() ?? '' };
    }
    categories.set(name, rules.map(unpack));
    if (name !== 'BLOCK') return entry.raw;
    blockCategories++;
    const kept = fields.filter(field => {
      if (field.field !== 2) return true;
      const { type, value } = unpack(field);
      if (value !== exception || (type !== 2 && type !== 3)) return true;
      removed++;
      return false;
    });
    categories.set(name, kept.filter(field => field.field === 2).map(unpack));
    if (kept.length === fields.length) return entry.raw;
    const patched = Buffer.concat(kept.map(field => field.raw));
    return Buffer.concat([Buffer.from([10]), encodeLength(patched.length), patched]);
  });
  if (blockCategories !== 1 || removed > 1) throw new Error('Unexpected BLOCK category structure');
  function matches(rule) {
    if (rule.type === 3) return exception === rule.value;
    if (rule.type === 2) return exception === rule.value || exception.endsWith('.' + rule.value);
    if (rule.type === 1) return new RegExp(rule.value, 'i').test(exception);
    return rule.type === 0 && exception.includes(rule.value);
  }
  if (!categories.get('RU')?.some(matches) || categories.get('BLOCK')?.some(matches)) {
    throw new Error('Expected the MapKit host to be direct and absent from BLOCK');
  }
  return { data: Buffer.concat(outputFields), categories, ruleMatches: matches, removed };
}

async function existingState() {
  try { return JSON.parse(await readFile(join(output, 'sync.json'), 'utf8')); }
  catch (error) {
    if (error.code !== 'ENOENT') throw error;
    return {};
  }
}

async function writeOutput(name, value) {
  const target = join(output, name);
  const temporary = target + '.tmp';
  await writeFile(temporary, value);
  await rename(temporary, target);
}

const raw = await download(upstream, 64 * 1024);
const profile = JSON.parse(raw);
const geoUrl = new URL(profile.Geositeurl);
const geoIpUrl = new URL(profile.Geoipurl);
if (geoUrl.origin + geoUrl.pathname !== sourceBase
    || geoIpUrl.origin + geoIpUrl.pathname !== 'https://cdn.jsdelivr.net/gh/SpeedeMiner/Geoip-ru-server@cdn/geoip_RU.dat'
    || profile.Name !== productionName
    || profile.RouteOrder !== 'block-proxy-direct'
    || !profile.DirectSites?.includes('geosite:ru')
    || !profile.BlockSites?.includes('geosite:block')) {
  throw new Error('Upstream profile changed unexpectedly');
}
const geosite = await download(geoUrl, 8 * 1024 * 1024);
const patched = patchGeosite(geosite);
for (const key of ['BlockSites', 'ProxySites', 'DirectSites']) {
  for (const rule of profile[key]) {
    if (rule.startsWith('geosite:')) {
      const category = patched.categories.get(rule.slice(8).toUpperCase());
      if (!category?.length) throw new Error('GeoSite category missing: ' + rule);
      if (key !== 'DirectSites' && category.some(patched.ruleMatches)) {
        throw new Error('An upstream ' + key + ' category overrides MapKit direct routing: ' + rule);
      }
    } else {
      const host = rule.replace(/^domain:|^full:/, '').toLowerCase();
      if (key !== 'DirectSites' && (exception === host || exception.endsWith('.' + host))) {
        throw new Error('An explicit upstream rule overrides MapKit direct routing');
      }
    }
  }
}

const digest = sha256(patched.data);
const input = sha256(Buffer.from(JSON.stringify([profile, digest, exception, publicBase, testName])));
const previous = await existingState();
const upstreamTime = Number(profile.LastUpdated);
if (!Number.isSafeInteger(upstreamTime) || upstreamTime < 1) throw new Error('Invalid upstream timestamp');
const timestamp = String(previous.input === input
  ? Number(previous.timestamp)
  : Math.max(upstreamTime + 1, (Number(previous.timestamp) || 0) + 1, Math.floor(Date.now() / 1000)));
if (!/^\d{10}$/.test(timestamp)) throw new Error('Invalid published timestamp');

const datName = 'geosite-' + digest.slice(0, 32) + '.dat';
const primary = { ...profile, Geositeurl: publicBase + '/' + datName, LastUpdated: timestamp, lastUpdatedDate: timestamp };
const trial = { ...primary, Name: testName };
const asJson = value => JSON.stringify(value, null, 2) + '\n';
await mkdir(output, { recursive: true });
await writeOutput(datName, patched.data);
await writeOutput('profile.json', asJson(primary));
await writeOutput('navigator-test.json', asJson(trial));
await writeOutput('navigator-test-happ.txt', 'happ://routing/onadd/' + Buffer.from(JSON.stringify(trial)).toString('base64') + '\n');
await writeOutput('sync.json', asJson({ input, timestamp, upstreamProfile: upstream, upstreamGeoSite: geoUrl.href, patchedGeoSiteSHA256: digest, removedBlockEntries: patched.removed }));
console.log('Verified MapKit route; removed ' + patched.removed + ' BLOCK rule; profile timestamp ' + timestamp);
