/* Product illustrations and filter icons, drawn as SVG.
 * The synthetic catalog has no photos, so every product family gets a drawn
 * glyph tinted by material (copper, brass, black iron, PVC...). In production
 * these become the fallback when a SKU has no image, and the same glyphs drive
 * the visual filters (type tiles, connection icons, material swatches).
 */
(function () {
  const MAT = {
    Copper: "#c7733f", Bronze: "#a9733d", Brass: "#cda349", Stainless: "#b8c1c9", "Black iron": "#3d4248",
    Galvanized: "#a3adb6", "Cast iron": "#52565b", CPVC: "#e7d6aa", PVC: "#eceef0", ABS: "#34363a",
    PEX: "#d9433c", "Carbon steel": "#6f7479", Poly: "#dcdcdc",
    _equip: "#e9ecef", _dark: "#4b5563", _elec: "#e5e7eb",
  };
  const hex = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
  const toHex = (a) => "#" + a.map((v) => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, "0")).join("");
  const shade = (h, f) => toHex(hex(h).map((v) => (f < 0 ? v * (1 + f) : v + (255 - v) * f)));
  let uid = 0;

  function materialColor(item) {
    if (item.material && MAT[item.material]) {
      if (item.material === "PEX") {
        const c = (item.attrs && item.attrs.color) || "";
        return c === "Blue" ? "#2f6fd1" : c === "White" ? "#eef0f2" : "#d9433c";
      }
      return MAT[item.material];
    }
    const f = item.family || "";
    const FAM = { cu_fitting_sweat: "Copper", cu_fitting_press: "Copper", cu_pipe: "Copper", iron_fitting: "Black iron",
      pvc_pressure: "PVC", dwv_fitting: "PVC", nohub: "Cast iron", ss_fitting: "Stainless", buttweld: "Carbon steel",
      flange: "Carbon steel", pex_fitting: "Brass", push_fitting: "Brass", ball_valve: "Brass", gate_check_valve: "Bronze",
      prv: "Bronze", backflow: "Bronze", relief_valve: "Brass", hangers: "Galvanized", waterworks: "Stainless", grooved: "Carbon steel" };
    if (FAM[f]) return MAT[FAM[f]];
    if (f === "pex_tubing") return "#d9433c";
    if (/^(wh_|boiler$|furnace|ac_|minisplit|expansion|toilet|sink|disposal)/.test(f)) return MAT._equip;
    if (/capacitor|contactor|thermostat|motor|hvac_parts/.test(f)) return MAT._elec;
    if (/gas/.test(f)) return "#e3b23c";
    if (/duct|grille|filter/.test(f)) return "#c9d0d6";
    if (/tools/.test(f)) return "#c0392b";
    return "#b8c1c9";
  }

  // tube drawn as outline + fill + highlight
  const tube = (d, c, w) => `<path d="${d}" fill="none" stroke="${shade(c, -0.45)}" stroke-width="${w + 2.5}" stroke-linecap="butt" stroke-linejoin="round"/>
    <path d="${d}" fill="none" stroke="${c}" stroke-width="${w}" stroke-linecap="butt" stroke-linejoin="round"/>
    <path d="${d}" fill="none" stroke="${shade(c, 0.45)}" stroke-width="${w * 0.22}" stroke-linecap="butt" stroke-linejoin="round" transform="translate(-${w * 0.18},-${w * 0.18})" opacity=".8"/>`;
  const box = (x, y, w, h, c, r = 3) => `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="${r}" fill="url(#g${uid})" stroke="${shade(c, -0.45)}" stroke-width="1.4"/>`;
  const sock = (x, y, w, h, c) => `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="1.5" fill="${shade(c, -0.08)}" stroke="${shade(c, -0.45)}" stroke-width="1.4"/>`;
  const hexnut = (cx, cy, r, c) => {
    const pts = [0, 1, 2, 3, 4, 5].map((k) => [cx + r * Math.cos(Math.PI / 3 * k), cy + r * Math.sin(Math.PI / 3 * k)].join(",")).join(" ");
    return `<polygon points="${pts}" fill="${shade(c, 0.1)}" stroke="${shade(c, -0.45)}" stroke-width="1.4"/>`;
  };
  const circ = (cx, cy, r, fill, st) => `<circle cx="${cx}" cy="${cy}" r="${r}" fill="${fill}" stroke="${st}" stroke-width="1.4"/>`;
  const lever = (col) => `<rect x="29" y="12" width="6" height="10" fill="#8a8f96" stroke="#555" stroke-width="1"/>
     <path d="M31 12 L54 8 L55 13 L33 17 Z" fill="${col}" stroke="${shade(col, -0.4)}" stroke-width="1.2"/>`;

  const G = {
    elbow90: (c) => tube("M16 56 V36 Q16 18 34 18 H56", c, 13) + sock(8, 46, 16, 12, c) + sock(46, 11, 12, 16, c),
    elbow45: (c) => tube("M10 54 L30 34 Q34 30 40 30 H58", c, 13) + sock(48, 23, 12, 14, c),
    tee: (c) => tube("M6 26 H58", c, 13) + tube("M32 30 V58", c, 13) + sock(4, 18, 10, 16, c) + sock(50, 18, 10, 16, c) + sock(24, 48, 16, 10, c),
    wye: (c) => tube("M6 40 H58", c, 13) + tube("M26 38 L48 14", c, 12) + sock(4, 32, 10, 16, c) + sock(50, 32, 10, 16, c),
    coupling: (c) => tube("M10 32 H54", c, 18) + `<rect x="30" y="21" width="4" height="22" fill="${shade(c, -0.25)}"/>`,
    reducer: (c) => `<path d="M8 20 H28 L40 26 H56 V38 H40 L28 44 H8 Z" fill="url(#g${uid})" stroke="${shade(c, -0.45)}" stroke-width="1.4"/>`,
    cap: (c) => `<path d="M14 18 H40 Q54 18 54 32 Q54 46 40 46 H14 Z" fill="url(#g${uid})" stroke="${shade(c, -0.45)}" stroke-width="1.4"/>`,
    adapter: (c) => sock(6, 20, 22, 24, c) + hexnut(34, 32, 11, c) + `<rect x="44" y="24" width="14" height="16" fill="${c}" stroke="${shade(c, -0.45)}" stroke-width="1.2"/>` +
      [47, 50, 53, 56].map((x) => `<line x1="${x}" y1="24" x2="${x - 2}" y2="40" stroke="${shade(c, -0.35)}" stroke-width="1"/>`).join(""),
    union: (c) => sock(4, 22, 16, 20, c) + sock(44, 22, 16, 20, c) + hexnut(32, 32, 15, c) + circ(32, 32, 6, shade(c, -0.15), shade(c, -0.45)),
    pipe: (c) => tube("M6 36 L50 22", c, 14) + `<ellipse cx="52" cy="21.5" rx="5" ry="7.5" transform="rotate(-18 52 21.5)" fill="${shade(c, -0.35)}" stroke="${shade(c, -0.5)}"/>`,
    coil: (c) => [24, 18, 12].map((r) => `<circle cx="32" cy="32" r="${r}" fill="none" stroke="${shade(c, -0.35)}" stroke-width="7"/><circle cx="32" cy="32" r="${r}" fill="none" stroke="${c}" stroke-width="5"/>`).join(""),
    ptrap: (c) => tube("M12 10 V34 Q12 50 28 50 Q44 50 44 34 V28 H58", c, 11),
    flange: (c) => circ(32, 32, 24, `url(#g${uid})`, shade(c, -0.45)) + circ(32, 32, 10, shade(c, -0.5), shade(c, -0.6)) +
      [0, 1, 2, 3, 4, 5, 6, 7].map((k) => circ(32 + 17 * Math.cos(k * Math.PI / 4), 32 + 17 * Math.sin(k * Math.PI / 4), 2.4, shade(c, -0.55), shade(c, -0.6))).join(""),
    ballvalve: (c) => box(12, 24, 40, 22, c, 5) + hexnut(10, 35, 8, c) + hexnut(54, 35, 8, c) + lever("#e0a82e"),
    gatevalve: (c) => box(12, 34, 40, 18, c, 4) + `<rect x="27" y="16" width="10" height="20" fill="${shade(c, -0.1)}" stroke="${shade(c, -0.45)}" stroke-width="1.3"/><rect x="31" y="8" width="2.5" height="10" fill="#777"/>` +
      `<ellipse cx="32" cy="8" rx="15" ry="4" fill="none" stroke="#c0392b" stroke-width="3"/>`,
    checkvalve: (c) => box(8, 22, 48, 22, c, 6) + `<path d="M20 33 H40 M34 27 L41 33 L34 39" stroke="${shade(c, -0.55)}" stroke-width="3" fill="none"/>`,
    prv: (c) => box(10, 36, 44, 16, c, 4) + `<path d="M20 36 Q20 12 32 12 Q44 12 44 36 Z" fill="${shade(c, 0.15)}" stroke="${shade(c, -0.45)}" stroke-width="1.4"/><rect x="30" y="4" width="4" height="9" fill="#777"/>`,
    backflow: (c) => box(6, 30, 52, 16, c, 4) + `<path d="M16 30 Q16 18 24 18 Q32 18 32 30Z M34 30 Q34 18 42 18 Q50 18 50 30Z" fill="${shade(c, 0.15)}" stroke="${shade(c, -0.45)}" stroke-width="1.3"/>` +
      `<circle cx="10" cy="52" r="3" fill="#2f6fd1"/><circle cx="54" cy="52" r="3" fill="#2f6fd1"/>`,
    relief: (c) => box(18, 26, 28, 22, c, 4) + sock(26, 48, 12, 12, c) + `<rect x="46" y="31" width="12" height="12" fill="${shade(c, -0.1)}" stroke="${shade(c, -0.45)}"/>` +
      `<path d="M28 26 V14 H46 V18 H32 V26" fill="#aab" stroke="#556" stroke-width="1.2"/>`,
    stop: (c) => tube("M14 44 H34 V20", c, 10) + `<ellipse cx="16" cy="44" rx="9" ry="6" fill="${shade(c, 0.2)}" stroke="${shade(c, -0.45)}"/><rect x="40" y="40" width="16" height="8" rx="2" fill="#e9ecef" stroke="#667"/>`,
    zonevalve: (c) => box(8, 42, 48, 14, c, 4) + `<rect x="16" y="10" width="32" height="30" rx="3" fill="#f3f4f6" stroke="#556" stroke-width="1.4"/><circle cx="32" cy="25" r="5" fill="#9aa"/>`,
    tank: (c) => `<rect x="18" y="8" width="28" height="50" rx="8" fill="url(#g${uid})" stroke="${shade(c, -0.4)}" stroke-width="1.5"/><rect x="18" y="26" width="28" height="7" fill="${shade(c, -0.12)}"/>
      <rect x="23" y="3" width="4" height="7" fill="#b87333"/><rect x="37" y="3" width="4" height="7" fill="#b87333"/><rect x="14" y="56" width="36" height="4" rx="1" fill="#9aa3ab"/><rect x="26" y="40" width="12" height="7" rx="1" fill="#374151"/>`,
    tankless: (c) => `<rect x="14" y="6" width="36" height="46" rx="4" fill="url(#g${uid})" stroke="${shade(c, -0.4)}" stroke-width="1.5"/><rect x="22" y="14" width="20" height="10" rx="2" fill="#1f2937"/><text x="32" y="22" font-size="7" text-anchor="middle" fill="#4ade80" font-family="monospace">120°</text>
      <rect x="20" y="52" width="4" height="8" fill="#b87333"/><rect x="30" y="52" width="4" height="8" fill="#e3b23c"/><rect x="40" y="52" width="4" height="8" fill="#b87333"/>`,
    boiler: (c) => `<rect x="12" y="12" width="40" height="46" rx="3" fill="url(#g${uid})" stroke="${shade(c, -0.4)}" stroke-width="1.5"/><rect x="18" y="18" width="28" height="8" rx="1.5" fill="#1f2937"/><circle cx="24" cy="36" r="4" fill="#c0392b"/><circle cx="36" cy="36" r="4" fill="#9aa3ab"/><rect x="28" y="4" width="8" height="9" fill="#9aa3ab" stroke="#667"/>`,
    xtank: (c) => `<rect x="18" y="12" width="28" height="40" rx="14" fill="${c === MAT._equip ? "#3b82c4" : c}" stroke="#1e3a5f" stroke-width="1.5"/><rect x="29" y="52" width="6" height="8" fill="#b8a14a"/>`,
    circulator: (c) => `<circle cx="28" cy="38" r="15" fill="#374151" stroke="#111" stroke-width="1.4"/><rect x="4" y="32" width="10" height="12" fill="#4b5563"/><rect x="42" y="32" width="10" height="12" fill="#4b5563"/><rect x="20" y="8" width="24" height="18" rx="3" fill="#2f6fd1" stroke="#1e3a5f"/>`,
    baseboard: (c) => `<rect x="4" y="22" width="56" height="22" rx="2" fill="url(#g${uid})" stroke="${shade(c, -0.4)}" stroke-width="1.4"/>` + [10, 16, 22, 28, 34, 40, 46, 52].map((x) => `<line x1="${x}" y1="26" x2="${x}" y2="40" stroke="${shade(c, -0.25)}" stroke-width="1.5"/>`).join(""),
    manifold: (c) => tube("M6 20 H58", "#b8c1c9", 9) + [14, 24, 34, 44, 54].map((x) => `<rect x="${x - 2.5}" y="24" width="5" height="12" fill="#9aa3ab"/><path d="M${x} 36 V58" stroke="${x % 20 ? "#d9433c" : "#2f6fd1"}" stroke-width="4"/>`).join(""),
    furnace: (c) => `<rect x="16" y="4" width="32" height="56" rx="2" fill="url(#g${uid})" stroke="${shade(c, -0.4)}" stroke-width="1.5"/>` + [40, 44, 48, 52].map((y) => `<line x1="22" y1="${y}" x2="42" y2="${y}" stroke="${shade(c, -0.3)}" stroke-width="1.5"/>`).join("") + `<rect x="22" y="12" width="20" height="8" rx="1" fill="#374151"/>`,
    condenser: (c) => `<rect x="8" y="10" width="48" height="46" rx="3" fill="url(#g${uid})" stroke="${shade(c, -0.4)}" stroke-width="1.5"/>` +
      circ(32, 30, 15, "#2d3136", "#111") + [0, 1, 2, 3].map((k) => `<path d="M32 30 L${32 + 13 * Math.cos(k * Math.PI / 2 + .4)} ${30 + 13 * Math.sin(k * Math.PI / 2 + .4)}" stroke="#9aa3ab" stroke-width="3"/>`).join("") +
      [48, 51, 54].map((y) => `<line x1="12" y1="${y}" x2="52" y2="${y}" stroke="${shade(c, -0.25)}"/>`).join(""),
    minisplit: (c) => `<rect x="4" y="20" width="56" height="22" rx="8" fill="url(#g${uid})" stroke="${shade(c, -0.4)}" stroke-width="1.5"/><path d="M12 36 H52" stroke="${shade(c, -0.35)}" stroke-width="2.5"/><circle cx="50" cy="27" r="1.6" fill="#22c55e"/>`,
    capacitor: (c) => `<rect x="18" y="16" width="28" height="42" rx="6" fill="url(#g${uid})" stroke="#667" stroke-width="1.4"/><rect x="18" y="16" width="28" height="7" rx="3" fill="#9aa3ab"/>` +
      [24, 32, 40].map((x) => `<rect x="${x - 2}" y="8" width="4" height="9" fill="#c0c6cc" stroke="#667" stroke-width=".8"/>`).join("") + `<text x="32" y="44" font-size="7" text-anchor="middle" fill="#374151" font-family="sans-serif" font-weight="700">MFD</text>`,
    contactor: (c) => `<rect x="12" y="12" width="40" height="42" rx="3" fill="#374151" stroke="#111"/><rect x="18" y="20" width="28" height="10" fill="#e5e7eb"/>` + [18, 30, 42].map((x) => `<rect x="${x}" y="40" width="6" height="8" fill="#c0c6cc"/>`).join(""),
    motor: (c) => `<rect x="10" y="18" width="36" height="28" rx="6" fill="#4b5563" stroke="#111"/>` + [22, 28, 34].map((x) => `<line x1="${x}" y1="18" x2="${x}" y2="46" stroke="#6b7280"/>`).join("") + `<rect x="46" y="29" width="12" height="5" fill="#9aa3ab"/><rect x="12" y="46" width="32" height="5" fill="#374151"/>`,
    thermostat: (c) => `<rect x="12" y="12" width="40" height="40" rx="10" fill="#f9fafb" stroke="#667" stroke-width="1.4"/><circle cx="32" cy="32" r="12" fill="#111827"/><text x="32" y="36" font-size="10" text-anchor="middle" fill="#fbbf24" font-family="sans-serif">68</text>`,
    cylinder: (c) => `<rect x="18" y="16" width="28" height="42" rx="12" fill="#e879a6" stroke="#9d174d" stroke-width="1.4"/><rect x="26" y="8" width="12" height="9" fill="#6b7280"/><rect x="18" y="30" width="28" height="10" fill="#f9fafb" opacity=".85"/>`,
    lineset: (c) => [20, 14].map((r) => `<circle cx="32" cy="32" r="${r}" fill="none" stroke="#1f2937" stroke-width="7"/><circle cx="32" cy="32" r="${r}" fill="none" stroke="#c7733f" stroke-width="2.5"/>`).join(""),
    duct: (c) => tube("M8 40 L52 22", c, 20) + `<ellipse cx="53" cy="21.5" rx="5" ry="10.5" transform="rotate(-22 53 21.5)" fill="${shade(c, -0.3)}" stroke="${shade(c, -0.5)}"/>`,
    grille: (c) => `<rect x="8" y="14" width="48" height="36" rx="2" fill="#f3f4f6" stroke="#667" stroke-width="1.4"/>` + [20, 26, 32, 38, 44].map((y) => `<line x1="12" y1="${y}" x2="52" y2="${y}" stroke="#9aa3ab" stroke-width="2"/>`).join(""),
    filter: (c) => `<rect x="10" y="12" width="44" height="40" fill="#f5f0e1" stroke="#8a7a52" stroke-width="1.6"/><path d="M14 16 L20 48 L26 16 L32 48 L38 16 L44 48 L50 16" fill="none" stroke="#c9b98a" stroke-width="1.6"/>`,
    toilet: (c) => `<rect x="34" y="8" width="20" height="24" rx="2" fill="#fdfdfd" stroke="#667" stroke-width="1.4"/><path d="M10 30 H48 Q48 46 36 48 L38 58 H22 L24 48 Q10 44 10 30 Z" fill="#fdfdfd" stroke="#667" stroke-width="1.4"/><rect x="8" y="27" width="40" height="4" rx="2" fill="#e5e7eb" stroke="#667"/>`,
    faucet: (c) => `<rect x="14" y="50" width="30" height="6" rx="2" fill="#c0c6cc" stroke="#555"/><path d="M28 50 V20 Q28 10 40 10 Q50 10 50 22 V26" fill="none" stroke="#7b8490" stroke-width="7" stroke-linecap="round"/><path d="M28 50 V20 Q28 10 40 10 Q50 10 50 22 V26" fill="none" stroke="#dfe3e8" stroke-width="3.5" stroke-linecap="round"/><rect x="18" y="36" width="10" height="4" rx="2" fill="#7b8490"/>`,
    sink: (c) => `<rect x="6" y="16" width="52" height="34" rx="6" fill="#dfe3e8" stroke="#667" stroke-width="1.4"/><rect x="12" y="21" width="40" height="24" rx="5" fill="#b8c1c9"/><circle cx="32" cy="33" r="3" fill="#4b5563"/>`,
    disposal: (c) => `<rect x="18" y="16" width="28" height="36" rx="5" fill="#1f2937" stroke="#000"/><rect x="14" y="10" width="36" height="7" rx="2" fill="#9aa3ab"/><rect x="44" y="30" width="14" height="6" fill="#9aa3ab"/><rect x="24" y="24" width="16" height="6" fill="#dc2626"/>`,
    pump: (c) => `<rect x="18" y="18" width="28" height="36" rx="4" fill="#2f6fd1" stroke="#1e3a5f" stroke-width="1.4"/><rect x="28" y="6" width="8" height="13" fill="#9aa3ab"/><path d="M46 44 L58 32" stroke="#374151" stroke-width="2"/><circle cx="58" cy="31" r="4" fill="#f59e0b"/>`,
    flexhose: (c) => `<path d="M8 50 Q8 20 32 20 Q56 20 56 44" fill="none" stroke="#b58a1f" stroke-width="9"/><path d="M8 50 Q8 20 32 20 Q56 20 56 44" fill="none" stroke="#e3b23c" stroke-width="6" stroke-dasharray="2 2"/>` + hexnut(8, 54, 6, "#cda349") + hexnut(56, 48, 6, "#cda349"),
    steamtrap: (c) => box(10, 28, 44, 20, c, 6) + `<rect x="20" y="12" width="24" height="17" rx="3" fill="${shade(c, 0.1)}" stroke="${shade(c, -0.45)}" stroke-width="1.3"/>`,
    clamp: (c) => tube("M4 32 H60", "#3b6ea5", 16) + `<rect x="22" y="18" width="20" height="28" rx="3" fill="${c}" stroke="${shade(c, -0.45)}"/>` + [24, 30, 36].map((x) => `<rect x="${x}" y="12" width="3" height="40" fill="#6b7280"/>`).join(""),
    hanger: (c) => `<rect x="30" y="4" width="4" height="22" fill="#6b7280"/><path d="M14 26 H50 V34 Q50 56 32 56 Q14 56 14 34 Z" fill="none" stroke="${c}" stroke-width="5"/>`,
    wrench: (c) => `<rect x="30" y="18" width="8" height="42" rx="3" fill="#c0392b" stroke="#7f1d1d" transform="rotate(-20 34 40)"/><path d="M18 8 H44 V20 H34 V26 H24 V20 H18 Z" fill="#6b7280" stroke="#374151" transform="rotate(-20 34 40)"/>`,
    can: (c) => `<rect x="18" y="12" width="28" height="44" rx="4" fill="#7c3aed" stroke="#4c1d95" stroke-width="1.4"/><rect x="18" y="26" width="28" height="16" fill="#fbbf24"/><rect x="24" y="6" width="16" height="7" rx="2" fill="#6b7280"/>`,
    drain: (c) => `<rect x="12" y="12" width="40" height="40" rx="3" fill="#c0c6cc" stroke="#555" stroke-width="1.4"/>` + [20, 26, 32, 38, 44].map((x) => `<rect x="${x - 1}" y="18" width="2.4" height="28" fill="#555"/>`).join(""),
    gear: (c) => `<g transform="translate(32 32)">` + [...Array(8)].map((_, k) => `<rect x="-3.5" y="-24" width="7" height="10" fill="#6b7280" transform="rotate(${k * 45})"/>`).join("") + `<circle r="16" fill="#9aa3ab" stroke="#4b5563" stroke-width="1.5"/><circle r="6" fill="#f3f4f6" stroke="#4b5563"/></g>`,
    igniter: (c) => `<rect x="26" y="6" width="12" height="30" rx="2" fill="#e9d5b0" stroke="#8a7a52"/><path d="M29 10 V32 M35 10 V32" stroke="#c2410c" stroke-width="1.5"/><rect x="24" y="36" width="16" height="8" fill="#f3f4f6" stroke="#667"/><path d="M28 44 Q24 56 12 58 M36 44 Q40 56 52 58" fill="none" stroke="#1f2937" stroke-width="2"/>`,
    board: (c) => `<rect x="8" y="12" width="48" height="40" rx="2" fill="#15803d" stroke="#14532d" stroke-width="1.4"/><rect x="16" y="20" width="12" height="10" fill="#111"/><rect x="34" y="22" width="14" height="6" fill="#111"/><rect x="16" y="38" width="30" height="5" fill="#fbbf24"/>`,
    cartridge: (c) => `<rect x="24" y="18" width="16" height="40" rx="3" fill="#dfe3e8" stroke="#667" stroke-width="1.4"/><rect x="29" y="6" width="6" height="13" fill="#b8c1c9" stroke="#667"/><rect x="24" y="34" width="16" height="4" fill="#111"/>`,
    butterfly: (c) => circ(32, 36, 18, `url(#g${uid})`, shade(c, -0.45)) + `<rect x="30" y="8" width="4" height="12" fill="#777"/><path d="M32 8 L56 6 L56 11 L32 12 Z" fill="#e0a82e"/>`,
  };

  function typeGlyph(t) {
    t = (t || "").toLowerCase();
    if (/p-trap|trap$/.test(t) && !/steam/.test(t)) return "ptrap";
    if (/ball valve/.test(t)) return "ballvalve";
    if (/flange/.test(t)) return "flange";
    if (/90|1\/4 bend|elbow|closet bend/.test(t) && !/45/.test(t)) return "elbow90";
    if (/45|1\/8 bend|1\/16 bend/.test(t)) return "elbow45";
    if (/wye|combo/.test(t)) return "wye";
    if (/tee|cross|test tee/.test(t)) return "tee";
    if (/cap|plug|cleanout/.test(t)) return "cap";
    if (/union/.test(t)) return "union";
    if (/reduc|bushing|transition/.test(t)) return "reducer";
    if (/adapter|stub out|drop ear/.test(t)) return "adapter";
    if (/nipple|pipe|stick/.test(t)) return "pipe";
    if (/coupling|sleeve/.test(t)) return "coupling";
    return null;
  }
  const PART_TYPES = [[/igniter|flame sensor|thermocouple|spark|electrode|flue sensor/, "igniter"], [/board|module|control$|transformer|defrost/, "board"],
    [/motor|blower|inducer|fan/, "motor"], [/capacitor/, "capacitor"], [/contactor|relay|switch|limit|aquastat/, "contactor"],
    [/gas valve|gas control|valve/, "relief"], [/cartridge|stem|spool/, "cartridge"], [/anode|element|dip tube|burner|tube/, "pipe"]];

  function productGlyph(item) {
    const f = item.family || "", t = ((item.attrs && item.attrs.type) || "").toLowerCase();
    const fitting = /cu_fitting|iron_fitting|pvc_pressure|ss_fitting|buttweld|grooved|pex_fitting|push_fitting|dwv_fitting|nohub/.test(f);
    if (fitting) {
      if (/valve/.test(t)) return /ball/.test(t) ? "ballvalve" : /check/.test(t) ? "checkvalve" : "gatevalve";
      if (/strainer/.test(t)) return "steamtrap";
      if (/water heater connector/.test(t)) return "flexhose";
      return typeGlyph(t) || (f === "push_fitting" || f === "grooved" ? "coupling" : f === "dwv_fitting" || f === "nohub" ? "wye" : "elbow90");
    }
    if (/_parts$|hvac_parts|boiler_parts/.test(f)) { for (const [re, g] of PART_TYPES) if (re.test(t)) return g; return "gear"; }
    const M = {
      cu_pipe: "pipe", pex_tubing: "coil", ball_valve: "ballvalve", prv: "prv", backflow: "backflow", relief_valve: "relief",
      stop_valve: "stop", wh_tank: "tank", wh_indirect: "tank", wh_tankless: "tankless", expansion_tank: "xtank", boiler: "boiler",
      circulator: "circulator", baseboard: "baseboard", radiant: "manifold", furnace: "furnace", ac_condenser: "condenser",
      minisplit: "minisplit", capacitor: "capacitor", contactor: "contactor", motor: "motor", thermostat: "thermostat",
      duct: "duct", grille: "grille", filter: "filter", toilet: "toilet", faucet: "faucet", sink: "sink", disposal: "disposal",
      pumps: "pump", well: "pump", gas: "flexhose", flange: "flange", steam: "steamtrap", waterworks: "clamp", hangers: "hanger",
      tools: "wrench", sundries: "can", drainage: "drain",
    };
    if (f === "gate_check_valve") return /check/.test(t) ? "checkvalve" : /butterfly/.test(t) ? "butterfly" : "gatevalve";
    if (f === "mixing_valve") return /zone valve|powerhead/.test(t) ? "zonevalve" : /separator/.test(t) ? "steamtrap" : "prv";
    if (f === "refrigerant") return /cylinder/.test(t) ? "cylinder" : /line set/.test(t) ? "lineset" : "adapter";
    if (f === "duct") return /elbow/.test(t) ? "elbow90" : /flex/.test(t) ? "coil" : "duct";
    return M[f] || "gear";
  }

  function svg(name, color, size = 64, cls = "") {
    uid++;
    const c = color || "#b8c1c9";
    const body = (G[name] || G.gear)(c);
    return `<svg class="${cls}" width="${size}" height="${size}" viewBox="0 0 64 64" aria-hidden="true"><defs><linearGradient id="g${uid}" x1="0" x2="0" y1="0" y2="1">
      <stop offset="0" stop-color="${shade(c, 0.35)}"/><stop offset=".55" stop-color="${c}"/><stop offset="1" stop-color="${shade(c, -0.25)}"/></linearGradient></defs>${body}</svg>`;
  }

  // ---- connection icons (filter rail)
  const CONN = {
    sweat: `<rect x="2" y="8" width="11" height="8" fill="#c7733f" stroke="#6b3a1f"/><rect x="11" y="6" width="11" height="12" rx="1" fill="#d68a55" stroke="#6b3a1f"/><path d="M12 18 q1 3 0 4" stroke="#9aa3ab" stroke-width="1.5" fill="none"/>`,
    press: `<rect x="2" y="8" width="11" height="8" fill="#c7733f" stroke="#6b3a1f"/><rect x="11" y="5" width="11" height="14" rx="2" fill="#d68a55" stroke="#6b3a1f"/><rect x="14" y="4" width="3" height="16" rx="1.5" fill="#b35f2e"/><circle cx="20" cy="12" r="1.3" fill="#16a34a"/>`,
    thread_m: `<rect x="2" y="8" width="20" height="8" fill="#cda349" stroke="#7a5f1d"/>` + [5, 9, 13, 17].map((x) => `<line x1="${x}" y1="8" x2="${x - 2}" y2="16" stroke="#7a5f1d"/>`).join(""),
    thread_f: `<rect x="2" y="5" width="20" height="14" rx="2" fill="#cda349" stroke="#7a5f1d"/><rect x="6" y="9" width="16" height="6" fill="#7a5f1d"/>` + [9, 13, 17].map((x) => `<line x1="${x}" y1="9" x2="${x - 2}" y2="15" stroke="#cda349"/>`).join(""),
    crimp: `<rect x="2" y="8" width="20" height="8" fill="#d9433c" stroke="#7f1d1d"/><rect x="10" y="6" width="6" height="12" rx="1" fill="#b8c1c9" stroke="#555"/>`,
    expansion: `<path d="M2 8 H12 L20 5 V19 L12 16 H2 Z" fill="#d9433c" stroke="#7f1d1d"/><rect x="12" y="5" width="4" height="14" fill="#eceef0" stroke="#666"/>`,
    push: `<rect x="2" y="8" width="11" height="8" fill="#c7733f" stroke="#6b3a1f"/><rect x="11" y="4" width="11" height="16" rx="3" fill="#cda349" stroke="#7a5f1d"/><rect x="11" y="7" width="3" height="10" fill="#1f2937"/>`,
    flanged: `<rect x="2" y="8" width="12" height="8" fill="#6f7479" stroke="#333"/><rect x="14" y="2" width="5" height="20" fill="#8a9097" stroke="#333"/><circle cx="16.5" cy="5" r="1" fill="#222"/><circle cx="16.5" cy="19" r="1" fill="#222"/>`,
    grooved: `<rect x="2" y="8" width="20" height="8" fill="#e05a2b" stroke="#7f1d1d"/><rect x="9" y="5" width="6" height="14" rx="2" fill="#f97316" stroke="#7c2d12"/>`,
    socket: `<rect x="2" y="8" width="11" height="8" fill="#eceef0" stroke="#666"/><rect x="11" y="5" width="11" height="14" rx="1" fill="#f7f7f7" stroke="#666"/>`,
    compression: `<rect x="2" y="9" width="20" height="6" fill="#b8c1c9" stroke="#555"/><polygon points="10,5 16,5 19,12 16,19 10,19 7,12" fill="#cda349" stroke="#7a5f1d"/>`,
  };
  function connKey(v) {
    v = (v || "").toLowerCase();
    if (/compression/.test(v)) return "compression";
    if (/press/.test(v)) return "press";
    if (/push/.test(v)) return "push";
    if (/crimp|clamp|cinch/.test(v)) return "crimp";
    if (/expansion|f1960/.test(v)) return "expansion";
    if (/flange/.test(v)) return "flanged";
    if (/groove/.test(v)) return "grooved";
    if (/compression/.test(v)) return "compression";
    if (/fip|fnpt|female/.test(v)) return "thread_f";
    if (/mip|thread|npt/.test(v)) return "thread_m";
    if (/sweat|solder|cxc|ftg/.test(v)) return "sweat";
    if (/socket|solvent/.test(v)) return "socket";
    return "socket";
  }
  function connIcon(v, size = 24) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" aria-hidden="true">${CONN[connKey(v)]}</svg>`;
  }
  function swatch(name, size = 18) {
    const c = MAT[name] || "#ccc";
    return `<span class="sw" style="width:${size}px;height:${size}px;background:radial-gradient(circle at 35% 30%, ${shade(c, 0.5)}, ${c} 55%, ${shade(c, -0.3)})"></span>`;
  }

  window.Glyphs = { svg, productGlyph, typeGlyph, materialColor, connIcon, swatch, MAT, shade,
    forItem: (item, size = 64, cls = "") => svg(productGlyph(item), materialColor(item), size, cls) };
})();
