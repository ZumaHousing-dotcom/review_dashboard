#!/usr/bin/env python3
"""Patch dashboard.html so it syncs Google + AppFolio reviews from Supabase every time it opens.

    python patch_dashboard.py dashboard.html

Safe to re-run: it refuses to patch a file that is already patched.
"""
import json
import sys
from pathlib import Path

OPEN = '<script type="__bundler/template">\n'
CLOSE = "\n  </script>"

NEW_SYNC = r"""  syncSupabase = async () => { // sync-v2
    if (this.state.syncing) return;
    this.setState({ syncing: true, upErr: '' });
    const URL_ = 'https://ykejzgmzebmkeoeqjkan.supabase.co', KEY = 'sb_publishable_qT-NrwPKfdoyJE4Q1tY-lQ_7vLvv5Hw';
    const H = { apikey: KEY, Authorization: 'Bearer ' + KEY };
    // Read a whole table/view a page at a time (Supabase caps each response at about 1000 rows).
    const pull = async path => {
      const all = [];
      for (let s = 0; s < 200000; ) {
        const r = await fetch(URL_ + path + '&limit=1000&offset=' + s, { headers: H });
        if (r.status === 404 && s === 0) return null; // table or view not created yet
        if (!r.ok) throw new Error('Supabase ' + r.status + ' on ' + path.split('?')[0] + ' (check the read policy)');
        const part = await r.json();
        if (!part.length) break;
        all.push(...part); s += part.length;
      }
      return all;
    };
    try {
      const A = await this.loadA();
      const [rows, pr, af] = await Promise.all([
        pull('/rest/v1/ri_reviews?select=*&order=created_at.desc,id.asc'),
        pull('/rest/v1/ri_properties?select=id,name'),
        pull('/rest/v1/ri_appfolio_feed?select=*&order=response_key.asc,question_no.asc')
      ]);
      if (!rows) throw new Error('ri_reviews not found in Supabase');
      const props = Object.fromEntries((pr || []).map(x => [x.id, x.name]));
      const g = rows.filter(r => r.source === 'google'), yl = rows.filter(r => r.source === 'yelp');
      const prev = this.state.fileData;
      const haveAf = !!(af && af.length);
      // Supabase is the source of truth: replace earlier Google/AppFolio imports instead of doubling them up.
      const existing = prev ? prev.reviews.filter(r => !r.id.startsWith('supabase-google:') && !r.id.startsWith('supabase-yelp:') && !(haveAf && r.source === 'AppFolio')) : [];
      const files = prev && prev.files ? prev.files.filter(f => f.name !== 'supabase-google' && f.name !== 'supabase-yelp' && !(haveAf && f.source === 'AppFolio')) : [];
      let out = null;
      const add = (args, extra) => {
        out = A.buildReviews({ ...args, meta: {}, fallbackProp: prev ? prev.prop : undefined, existing: out ? out.reviews : existing });
        files.push({ name: args.fileName, source: args.sourceOverride, count: out.added, ...extra });
      };
      if (g.length) {
        const header = ['Review text', 'Rating', 'Date', 'Author', 'Property'];
        const dataRows = g.map(r => [r.text || '', r.rating, r.created_at ? new Date(r.created_at) : '', r.author || '', props[r.property_id] || 'Zuma Housing']);
        add({ dataRows, header, mapping: header, fileName: 'supabase-google', sourceOverride: 'Google' });
      }
      if (yl.length) {
        const header = ['Review text', 'Rating', 'Date', 'Author', 'Property'];
        const dataRows = yl.map(r => [r.text || '', r.rating, r.created_at ? new Date(r.created_at) : '', r.author || '', props[r.property_id] || 'Zuma Housing']);
        add({ dataRows, header, mapping: header, fileName: 'supabase-yelp', sourceOverride: 'Yelp' });
      }
      if (haveAf) {
        // The feed has one row per question answered; the dashboard wants one row per survey, one column per question.
        const TYPE = { maintenance: 'Maintenance', 'move-in': 'Move In', 'move-out': 'Move Out', showing: 'Showing', showings: 'Showing' };
        const groups = {}, seen = {};
        for (const x of af) {
          if (x.answer == null || String(x.answer).trim() === '') continue;
          const type = TYPE[String(x.survey_type || '').toLowerCase().trim().replace(/[ _]+/g, '-')];
          if (!type) continue;
          const grp = groups[type] || (groups[type] = { qs: {}, resp: {} });
          const sk = x.response_key + '|' + x.question_no;
          seen[sk] = seen[sk] === undefined ? 0 : seen[sk] + 1; // same question twice for one key = a second survey
          const rk = x.response_key + '#' + seen[sk];
          grp.qs[x.question_no] = grp.qs[x.question_no] || x.question;
          const r = grp.resp[rk] || (grp.resp[rk] = { prop: x.property, date: x.date_received, staff: x.assigned_user, a: {} });
          r.a[x.question_no] = x.answer;
        }
        for (const type of Object.keys(groups)) {
          const grp = groups[type], qn = Object.keys(grp.qs).sort((a, b) => a - b);
          const header = ['Property', 'Date received', 'Assigned user'].concat(qn.map(q => grp.qs[q]));
          let dataRows = Object.values(grp.resp).map(r => { const d = new Date(r.date); return [r.prop || '', d.getTime() ? d : '', r.staff || ''].concat(qn.map(q => r.a[q] === undefined ? '' : r.a[q])); });
          const mapping = A.applySurvey(header, A.guessMapping(header, dataRows), type), ri = mapping.indexOf('Rating');
          if (ri < 0) continue;
          dataRows = dataRows.filter(r => r[ri] !== '' && !isNaN(Number(r[ri]))); // a survey with no star score would otherwise count as 1 star
          if (!dataRows.length) continue;
          add({ dataRows, header, mapping, fileName: 'supabase-appfolio-' + type.replace(/ /g, '').toLowerCase(), sourceOverride: 'AppFolio', surveyTypeOverride: type }, { surveyType: type });
        }
      }
      if (!out) throw new Error('No reviews in Supabase yet');
      out.files = files;
      this.persist(out);
      this.setState({ fileData: out, syncing: false, lastSync: 'Just now', range: 'all', staffSel: this.state.staffSel || (out.staff[0] ? out.staff[0].id : '') });
    } catch (e) { this.setState({ syncing: false, upErr: 'Sync failed: ' + e.message }); }
  };
"""

OLD_SAVED_TAIL = ".then(() => this.importTakeout()).then(() => this.importAfNew());"
NEW_SAVED_TAIL = ".then(() => this.importTakeout()).then(() => this.importAfNew()).then(() => this.syncSupabase());"
OLD_FIRST_VISIT = "else if (!(this.props && this.props.initialView)) this.loadTestFiles();"
NEW_FIRST_VISIT = "else if (!(this.props && this.props.initialView)) Promise.resolve(this.loadTestFiles()).then(() => this.syncSupabase());"


def main(path):
    p = Path(path)
    html = p.read_text(encoding="utf-8")
    i = html.index(OPEN) + len(OPEN)
    j = html.index(CLOSE, i)
    js = json.loads(html[i:j])
    if "// sync-v2" in js:
        sys.exit("Already patched with the latest version; nothing to do.")
    a = js.index("  syncSupabase = async () => {")
    b = js.index("  importAfNew = async", a)
    js = js[:a] + NEW_SYNC + js[b:]
    # Start-up hooks: apply once (they are already present if an earlier version was patched in).
    for old, new in ((OLD_SAVED_TAIL, NEW_SAVED_TAIL), (OLD_FIRST_VISIT, NEW_FIRST_VISIT)):
        if js.count(old) == 1:
            js = js.replace(old, new)
        elif js.count(new) != 1:
            sys.exit(f"Dashboard code is not what I expected (around: {old[:40]}...). Not patching.")
    raw = json.dumps(js, ensure_ascii=False).replace("</", "<\\u002F")
    p.write_text(html[:i] + raw + html[j:], encoding="utf-8")
    print(f"patched {path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "dashboard.html")
