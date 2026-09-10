/* Minimal QR code encoder (byte mode, error correction level L or M,
   versions 1 to 40) so the lobby can show a scannable link without any
   library or network.  Follows ISO/IEC 18004; structure after the public
   domain reference implementations.  Usage:

     QR.svg("https://example.org/", { ecl: "M", module: 4, margin: 2 })  -> SVG markup
     QR.encode(text, ecl)  -> { size, modules }  where modules[y][x] is true for dark */

window.QR = (function () {
  "use strict";

  const ECC_CODEWORDS_PER_BLOCK = {
    L: [-1, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24, 28, 30, 28, 28, 28, 28, 30, 30, 26, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30],
    M: [-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26, 26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28],
  };
  const NUM_ERROR_CORRECTION_BLOCKS = {
    L: [-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4, 6, 6, 6, 6, 7, 8, 8, 9, 9, 10, 12, 12, 12, 13, 14, 15, 16, 17, 18, 19, 19, 20, 21, 22, 24, 25],
    M: [-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16, 17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49],
  };
  const ECL_BITS = { L: 1, M: 0 };

  function getBit(x, i) { return ((x >>> i) & 1) !== 0; }

  function numRawDataModules(ver) {
    let result = (16 * ver + 128) * ver + 64;
    if (ver >= 2) {
      const numAlign = Math.floor(ver / 7) + 2;
      result -= (25 * numAlign - 10) * numAlign - 55;
      if (ver >= 7) result -= 36;
    }
    return result;
  }
  function numDataCodewords(ver, ecl) {
    return Math.floor(numRawDataModules(ver) / 8) - ECC_CODEWORDS_PER_BLOCK[ecl][ver] * NUM_ERROR_CORRECTION_BLOCKS[ecl][ver];
  }

  /* ---- Reed-Solomon over GF(2^8) with the QR polynomial 0x11D ---- */
  function gfMul(x, y) {
    let z = 0;
    for (let i = 7; i >= 0; i--) {
      z = (z << 1) ^ ((z >>> 7) * 0x11d);
      z ^= ((y >>> i) & 1) * x;
    }
    return z & 0xff;
  }
  function rsDivisor(degree) {
    const result = new Array(degree).fill(0);
    result[degree - 1] = 1;
    let root = 1;
    for (let i = 0; i < degree; i++) {
      for (let j = 0; j < degree; j++) {
        result[j] = gfMul(result[j], root);
        if (j + 1 < degree) result[j] ^= result[j + 1];
      }
      root = gfMul(root, 0x02);
    }
    return result;
  }
  function rsRemainder(data, divisor) {
    const result = new Array(divisor.length).fill(0);
    for (const b of data) {
      const factor = b ^ result.shift();
      result.push(0);
      divisor.forEach((coef, i) => { result[i] ^= gfMul(coef, factor); });
    }
    return result;
  }

  /* ---- data codewords for byte mode ---- */
  function utf8Bytes(text) {
    return Array.from(new TextEncoder().encode(text));
  }
  function chooseVersion(nBytes, ecl) {
    for (let ver = 1; ver <= 40; ver++) {
      const cc = ver <= 9 ? 8 : 16;
      if (4 + cc + 8 * nBytes <= numDataCodewords(ver, ecl) * 8) return ver;
    }
    throw new Error("text too long for a QR code");
  }
  function makeDataCodewords(bytes, ver, ecl) {
    const bits = [];
    const push = (val, n) => { for (let i = n - 1; i >= 0; i--) bits.push((val >>> i) & 1); };
    push(0x4, 4);                               // byte mode
    push(bytes.length, ver <= 9 ? 8 : 16);
    for (const b of bytes) push(b, 8);
    const capacity = numDataCodewords(ver, ecl) * 8;
    push(0, Math.min(4, capacity - bits.length));   // terminator
    while (bits.length % 8 !== 0) bits.push(0);
    for (let pad = 0xec; bits.length < capacity; pad ^= 0xec ^ 0x11) push(pad, 8);
    const out = [];
    for (let i = 0; i < bits.length; i += 8) {
      let v = 0;
      for (let j = 0; j < 8; j++) v = (v << 1) | bits[i + j];
      out.push(v);
    }
    return out;
  }
  function addEccAndInterleave(data, ver, ecl) {
    const numBlocks = NUM_ERROR_CORRECTION_BLOCKS[ecl][ver];
    const blockEccLen = ECC_CODEWORDS_PER_BLOCK[ecl][ver];
    const rawCodewords = Math.floor(numRawDataModules(ver) / 8);
    const numShortBlocks = numBlocks - (rawCodewords % numBlocks);
    const shortBlockLen = Math.floor(rawCodewords / numBlocks);
    const blocks = [];
    const divisor = rsDivisor(blockEccLen);
    for (let i = 0, k = 0; i < numBlocks; i++) {
      const dat = data.slice(k, k + shortBlockLen - blockEccLen + (i < numShortBlocks ? 0 : 1));
      k += dat.length;
      const ecc = rsRemainder(dat, divisor);
      if (i < numShortBlocks) dat.push(0);
      blocks.push(dat.concat(ecc));
    }
    const result = [];
    for (let i = 0; i < blocks[0].length; i++) {
      blocks.forEach((block, j) => {
        if (i !== shortBlockLen - blockEccLen || j >= numShortBlocks) result.push(block[i]);
      });
    }
    return result;
  }

  /* ---- the symbol ---- */
  function encode(text, ecl) {
    ecl = ecl === "L" ? "L" : "M";
    const bytes = utf8Bytes(text);
    const ver = chooseVersion(bytes.length, ecl);
    const size = ver * 4 + 17;
    const modules = Array.from({ length: size }, () => new Array(size).fill(false));
    const isFunction = Array.from({ length: size }, () => new Array(size).fill(false));

    function setFn(x, y, dark) { modules[y][x] = dark; isFunction[y][x] = true; }

    function drawFinder(x, y) {
      for (let dy = -4; dy <= 4; dy++) {
        for (let dx = -4; dx <= 4; dx++) {
          const dist = Math.max(Math.abs(dx), Math.abs(dy));
          const xx = x + dx, yy = y + dy;
          if (xx >= 0 && xx < size && yy >= 0 && yy < size) setFn(xx, yy, dist !== 2 && dist !== 4);
        }
      }
    }
    function drawAlign(x, y) {
      for (let dy = -2; dy <= 2; dy++) for (let dx = -2; dx <= 2; dx++) setFn(x + dx, y + dy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1);
    }
    function alignPositions() {
      if (ver === 1) return [];
      const numAlign = Math.floor(ver / 7) + 2;
      const step = ver === 32 ? 26 : Math.ceil((ver * 4 + 4) / (numAlign * 2 - 2)) * 2;
      const result = [6];
      for (let pos = size - 7; result.length < numAlign; pos -= step) result.splice(1, 0, pos);
      return result;
    }
    function drawFormatBits(mask) {
      const data = (ECL_BITS[ecl] << 3) | mask;
      let rem = data;
      for (let i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >>> 9) * 0x537);
      const bits = ((data << 10) | rem) ^ 0x5412;
      for (let i = 0; i <= 5; i++) setFn(8, i, getBit(bits, i));
      setFn(8, 7, getBit(bits, 6));
      setFn(8, 8, getBit(bits, 7));
      setFn(7, 8, getBit(bits, 8));
      for (let i = 9; i < 15; i++) setFn(14 - i, 8, getBit(bits, i));
      for (let i = 0; i < 8; i++) setFn(size - 1 - i, 8, getBit(bits, i));
      for (let i = 8; i < 15; i++) setFn(8, size - 15 + i, getBit(bits, i));
      setFn(8, size - 8, true);
    }
    function drawVersion() {
      if (ver < 7) return;
      let rem = ver;
      for (let i = 0; i < 12; i++) rem = (rem << 1) ^ ((rem >>> 11) * 0x1f25);
      const bits = (ver << 12) | rem;
      for (let i = 0; i < 18; i++) {
        const bit = getBit(bits, i);
        const a = size - 11 + (i % 3), b = Math.floor(i / 3);
        setFn(a, b, bit);
        setFn(b, a, bit);
      }
    }
    function drawFunctionPatterns() {
      for (let i = 0; i < size; i++) { setFn(6, i, i % 2 === 0); setFn(i, 6, i % 2 === 0); }
      drawFinder(3, 3); drawFinder(size - 4, 3); drawFinder(3, size - 4);
      const pos = alignPositions();
      const n = pos.length;
      for (let i = 0; i < n; i++) {
        for (let j = 0; j < n; j++) {
          if (!((i === 0 && j === 0) || (i === 0 && j === n - 1) || (i === n - 1 && j === 0))) drawAlign(pos[i], pos[j]);
        }
      }
      drawFormatBits(0);
      drawVersion();
    }
    function drawCodewords(data) {
      let i = 0;
      for (let right = size - 1; right >= 1; right -= 2) {
        if (right === 6) right = 5;
        for (let vert = 0; vert < size; vert++) {
          for (let j = 0; j < 2; j++) {
            const x = right - j;
            const upward = ((right + 1) & 2) === 0;
            const y = upward ? size - 1 - vert : vert;
            if (!isFunction[y][x] && i < data.length * 8) {
              modules[y][x] = getBit(data[i >>> 3], 7 - (i & 7));
              i++;
            }
          }
        }
      }
    }
    function applyMask(mask) {
      for (let y = 0; y < size; y++) {
        for (let x = 0; x < size; x++) {
          let invert;
          switch (mask) {
            case 0: invert = (x + y) % 2 === 0; break;
            case 1: invert = y % 2 === 0; break;
            case 2: invert = x % 3 === 0; break;
            case 3: invert = (x + y) % 3 === 0; break;
            case 4: invert = (Math.floor(x / 3) + Math.floor(y / 2)) % 2 === 0; break;
            case 5: invert = ((x * y) % 2) + ((x * y) % 3) === 0; break;
            case 6: invert = (((x * y) % 2) + ((x * y) % 3)) % 2 === 0; break;
            default: invert = (((x + y) % 2) + ((x * y) % 3)) % 2 === 0; break;
          }
          if (!isFunction[y][x] && invert) modules[y][x] = !modules[y][x];
        }
      }
    }
    function penalty() {
      let score = 0;
      const line = (get) => {
        // runs of same colour, and finder-like patterns
        let s = "";
        let run = 1;
        for (let i = 0; i < size; i++) {
          s += get(i) ? "1" : "0";
          if (i > 0 && get(i) === get(i - 1)) { run++; if (run === 5) score += 3; else if (run > 5) score += 1; }
          else run = 1;
        }
        for (let idx = s.indexOf("10111010000"); idx >= 0; idx = s.indexOf("10111010000", idx + 1)) score += 40;
        for (let idx = s.indexOf("00001011101"); idx >= 0; idx = s.indexOf("00001011101", idx + 1)) score += 40;
      };
      for (let y = 0; y < size; y++) line((x) => modules[y][x]);
      for (let x = 0; x < size; x++) line((y) => modules[y][x]);
      for (let y = 0; y < size - 1; y++) {
        for (let x = 0; x < size - 1; x++) {
          const c = modules[y][x];
          if (c === modules[y][x + 1] && c === modules[y + 1][x] && c === modules[y + 1][x + 1]) score += 3;
        }
      }
      let dark = 0;
      for (const row of modules) for (const m of row) if (m) dark++;
      const total = size * size;
      const k = Math.ceil(Math.abs(dark * 20 - total * 10) / total) - 1;
      score += k * 10;
      return score;
    }

    drawFunctionPatterns();
    drawCodewords(addEccAndInterleave(makeDataCodewords(bytes, ver, ecl), ver, ecl));
    let best = 0, bestScore = Infinity;
    for (let m = 0; m < 8; m++) {
      applyMask(m); drawFormatBits(m);
      const sc = penalty();
      if (sc < bestScore) { bestScore = sc; best = m; }
      applyMask(m);                              // undo
    }
    applyMask(best);
    drawFormatBits(best);
    return { size, modules, version: ver, mask: best };
  }

  function svg(text, opts) {
    opts = opts || {};
    const q = encode(text, opts.ecl);
    const unit = opts.module || 4;
    const margin = opts.margin === undefined ? 2 : opts.margin;
    const dim = (q.size + margin * 2) * unit;
    let d = "";
    for (let y = 0; y < q.size; y++) {
      for (let x = 0; x < q.size; x++) {
        if (q.modules[y][x]) d += `M${(x + margin) * unit} ${(y + margin) * unit}h${unit}v${unit}h-${unit}z`;
      }
    }
    return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${dim} ${dim}" width="${dim}" height="${dim}" shape-rendering="crispEdges" role="img" aria-label="QR code">` +
      `<rect width="${dim}" height="${dim}" fill="#fff"/><path d="${d}" fill="#000"/></svg>`;
  }

  return { encode, svg };
})();
