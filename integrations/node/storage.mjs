// Local-only storage. No uploads, shell commands, or overwrite of existing captures.
import fs from 'node:fs';
import path from 'node:path';
import { randomUUID } from 'node:crypto';

export function sessionDirectory(workspaceRoot) {
  if (typeof workspaceRoot !== 'string' || !path.isAbsolute(workspaceRoot)) throw Error('invalid_root');
  const root = path.resolve(workspaceRoot);
  // Walk ancestors as well: an explicit path must not bypass a symlink/junction.
  let cursor = path.parse(root).root;
  for (const part of root.slice(cursor.length).split(path.sep).filter(Boolean)) {
    cursor = path.join(cursor, part);
    if (fs.lstatSync(cursor).isSymbolicLink() || !fs.statSync(cursor).isDirectory()) throw Error('unsafe_root');
  }
  let directory = root;
  for (const part of ['diagnostics', 'performance']) {
    directory = path.join(directory, part);
    try { fs.mkdirSync(directory, { mode: 0o700 }); } catch (error) { if (error.code !== 'EEXIST') throw error; }
    const info = fs.lstatSync(directory);
    if (!info.isDirectory() || info.isSymbolicLink()) throw Error('unsafe_directory');
  }
  const ignore = path.join(directory, '.gitignore');
  try { fs.writeFileSync(ignore, '*\n', { flag: 'wx', mode: 0o600 }); }
  catch (error) {
    if (error.code !== 'EEXIST') throw error;
    if (!fs.lstatSync(ignore).isFile() || fs.lstatSync(ignore).isSymbolicLink() || fs.lstatSync(ignore).size > 1024 ||
        fs.readFileSync(ignore, 'utf8').trim() !== '*') throw Error('capture_gitignore_required');
  }
  const session = path.join(directory, randomUUID());
  fs.mkdirSync(session, { mode: 0o700 });
  return session;
}

export function withinStructureBudget(value, maximum) {
  // Mirror the offline JSON value/depth budgets without recursive graph slicing.
  let count = 0;
  const stack = [[value, 0]];
  while (stack.length) {
    const [item, depth] = stack.pop();
    if (++count > maximum || depth > 64) return false;
    if (typeof item === 'number' && !Number.isFinite(item)) return false;
    if (item && typeof item === 'object') {
      const values = Object.values(item);
      if (values.length + count > maximum) return false;
      for (const next of values) stack.push([next, depth + 1]);
    }
  }
  return true;
}

export function writeJson(directory, name, value, maxBytes) {
  if (!/^[a-z-]+\.json$/.test(name)) throw Error('invalid_name');
  const text = JSON.stringify(value);
  if (Buffer.byteLength(text) > maxBytes) throw Error('size_limit');
  if (fs.lstatSync(directory).isSymbolicLink()) throw Error('unsafe_directory');
  // Exclusive publication: a .partial file never masquerades as a completed JSON capture.
  const temporary = path.join(directory, `${name}.${randomUUID()}.partial`);
  fs.writeFileSync(temporary, text, { flag: 'wx', mode: 0o600 });
  const destination = path.join(directory, name);
  // A hard link publishes complete bytes without replacing a pre-existing destination.
  // NTFS and ordinary POSIX filesystems support this; unsupported filesystems fail closed.
  fs.linkSync(temporary, destination);
  fs.unlinkSync(temporary);
  return destination;
}
