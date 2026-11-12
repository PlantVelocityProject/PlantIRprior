import { readFile, writeFile, mkdir, copyFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const destination = path.join(root, 'assets', 'fonts');
await mkdir(destination, { recursive: true });
const families = [
  ['geist', ['index.css', 'wght-italic.css']],
  ['noto-sans-sc', ['index.css']],
];
const styles = ['/* Generated from the pinned Fontsource packages by npm run fonts. */'];
const manifest = [];
for (const [family, entries] of families) {
  const packageRoot = path.join(root, 'node_modules', '@fontsource-variable', family);
  const metadata = JSON.parse(await readFile(path.join(packageRoot, 'package.json'), 'utf8'));
  const files = new Set();
  for (const entry of entries) {
    let css = await readFile(path.join(packageRoot, entry), 'utf8');
    // Keep the public CSS family names independent of the package naming suffix.
    css = css.replace(/font-family: '([^']+) Variable'/g, "font-family: '$1'");
    css = css.replace(/url\(\.\/files\/([^)]*)\)/g, (_, filename) => {
      files.add(filename);
      return `url('./${filename}')`;
    });
    styles.push(css);
  }
  for (const filename of files) {
    await copyFile(path.join(packageRoot, 'files', filename), path.join(destination, filename));
  }
  await copyFile(path.join(packageRoot, 'LICENSE'), path.join(destination, `${family}-LICENSE.txt`));
  manifest.push({ package: metadata.name, version: metadata.version, files: [...files] });
}
await writeFile(path.join(destination, 'fonts.css'), styles.join('\n'), 'utf8');
await writeFile(path.join(destination, 'fontsource-manifest.json'), JSON.stringify(manifest, null, 2) + '\n');
console.log(`Copied ${manifest.reduce((total, family) => total + family.files.length, 0)} Fontsource WOFF2 files and their licenses.`);
