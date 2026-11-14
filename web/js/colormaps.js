const STOPS = {
  viridis: [
    [68, 1, 84], [72, 40, 120], [62, 74, 137], [49, 104, 142], [38, 130, 142],
    [31, 158, 137], [53, 183, 121], [109, 205, 89], [180, 222, 44], [253, 231, 37],
  ],
  rdbu: [
    [103, 0, 31], [178, 24, 43], [214, 96, 77], [244, 165, 130], [253, 219, 199],
    [247, 247, 247], [209, 229, 240], [146, 197, 222], [67, 147, 195], [33, 102, 172], [5, 48, 97],
  ],
  cividis: [
    [0, 32, 77], [0, 52, 110], [57, 72, 108], [87, 92, 109], [112, 113, 115],
    [138, 135, 120], [165, 158, 116], [195, 183, 105], [225, 208, 87], [255, 234, 70],
  ],
  elevation: [
    [49, 54, 149], [69, 117, 180], [116, 173, 209], [171, 217, 233], [224, 243, 248],
    [254, 224, 144], [253, 174, 97], [244, 109, 67], [215, 48, 39], [165, 0, 38],
  ],
};

const LUTS = new Map();

function lookupTable(name) {
  if (!LUTS.has(name)) {
    const stops = STOPS[name];
    const table = new Uint8ClampedArray(256 * 3);
    for (let k = 0; k < 256; k += 1) {
      const position = (k / 255) * (stops.length - 1);
      const low = Math.floor(position);
      const high = Math.min(stops.length - 1, low + 1);
      const t = position - low;
      for (let channel = 0; channel < 3; channel += 1) {
        table[k * 3 + channel] = stops[low][channel] + t * (stops[high][channel] - stops[low][channel]);
      }
    }
    LUTS.set(name, table);
  }
  return LUTS.get(name);
}

export function writeColor(name, t, target, offset) {
  const table = lookupTable(name);
  const k = Math.round(Math.min(1, Math.max(0, t)) * 255) * 3;
  target[offset] = table[k];
  target[offset + 1] = table[k + 1];
  target[offset + 2] = table[k + 2];
}

export function gradientCss(name) {
  const stops = STOPS[name];
  const parts = stops.map((color, index) => `rgb(${color.join(",")}) ${(index / (stops.length - 1)) * 100}%`);
  return `linear-gradient(90deg, ${parts.join(", ")})`;
}
