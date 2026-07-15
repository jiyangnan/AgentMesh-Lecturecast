import {createHash} from 'node:crypto';
import {readFile, writeFile} from 'node:fs/promises';
import {dirname, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const definitionsPath = resolve(root, 'templates/remotion/src/director/component-definitions.json');
const outputs = [
  resolve(root, 'templates/remotion/src/director/component-catalog.json'),
  resolve(root, 'src/lecturecast/component-catalog.json'),
];
const lockPath = resolve(root, 'src/lecturecast/component-catalog.lock');

const stable = (value) => {
  if (Array.isArray(value)) return value.map(stable);
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.keys(value).sort().map((key) => [key, stable(value[key])]));
  }
  return value;
};
const canonicalBytes = (value) => Buffer.from(JSON.stringify(stable(value)));
const digest = (value) => `sha256:${createHash('sha256').update(canonicalBytes(value)).digest('hex')}`;

const validatePrimitive = (schema, value, location) => {
  if (schema.type === 'string') {
    if (typeof value !== 'string') throw new Error(`${location} must be a string`);
    if (schema.minLength !== undefined && value.length < schema.minLength) throw new Error(`${location} too short`);
    if (schema.maxLength !== undefined && value.length > schema.maxLength) throw new Error(`${location} too long`);
  } else if (schema.type === 'integer') {
    if (!Number.isInteger(value)) throw new Error(`${location} must be an integer`);
    if (schema.minimum !== undefined && value < schema.minimum) throw new Error(`${location} too small`);
    if (schema.maximum !== undefined && value > schema.maximum) throw new Error(`${location} too large`);
  } else if (schema.type === 'array') {
    if (!Array.isArray(value)) throw new Error(`${location} must be an array`);
    if (value.length < schema.minItems || value.length > schema.maxItems) throw new Error(`${location} item count invalid`);
    value.forEach((item, index) => validatePrimitive(schema.items, item, `${location}[${index}]`));
  }
};

const validateFixture = (component) => {
  const {props_schema: schema, preview_fixture: fixture} = component;
  for (const key of schema.required) {
    if (!(key in fixture)) throw new Error(`${component.component_id} missing preview prop ${key}`);
  }
  if (schema.additionalProperties === false) {
    for (const key of Object.keys(fixture)) {
      if (!(key in schema.properties)) throw new Error(`${component.component_id} has unknown preview prop ${key}`);
    }
  }
  for (const [key, value] of Object.entries(fixture)) {
    validatePrimitive(schema.properties[key], value, `${component.component_id}.${key}`);
  }
};

const definitions = JSON.parse(await readFile(definitionsPath, 'utf8'));
const ids = definitions.components.map((component) => component.component_id);
if (new Set(ids).size !== ids.length) throw new Error('duplicate component_id');
definitions.components.forEach(validateFixture);
const catalog = {
  catalog_version: definitions.catalog_version,
  components: [...definitions.components].sort((a, b) => a.component_id.localeCompare(b.component_id)),
};
const lock = {
  catalog_version: catalog.catalog_version,
  component_count: catalog.components.length,
  catalog_digest: digest(catalog),
};
const formattedCatalog = `${JSON.stringify(catalog, null, 2)}\n`;
const formattedLock = `${JSON.stringify(lock, null, 2)}\n`;

if (process.argv.includes('--check')) {
  for (const output of outputs) {
    if (await readFile(output, 'utf8') !== formattedCatalog) throw new Error(`${output} is stale`);
  }
  if (await readFile(lockPath, 'utf8') !== formattedLock) throw new Error(`${lockPath} is stale`);
} else {
  await Promise.all(outputs.map((output) => writeFile(output, formattedCatalog)));
  await writeFile(lockPath, formattedLock);
}
process.stdout.write(`${JSON.stringify(lock)}\n`);
