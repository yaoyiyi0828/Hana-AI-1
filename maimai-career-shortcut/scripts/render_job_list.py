"""Render referral-first company cards from a limited, verified response.

Supports the current companies schema and legacy jobs-only responses. Uses only
the Python standard library. Does not fetch data, contact people or apply to jobs.
"""
import argparse
import base64
import html
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

ROOT = Path(__file__).resolve().parents[1]
HR_FALLBACK_LABEL = "前往脉脉，搜索“HR”，进入大厂急招HR专场"


def esc(value):
    return html.escape(str(value or ''), quote=True)


def source_link(url, label, css=''):
    parsed = urlparse(str(url or ''))
    if parsed.scheme != 'https' or parsed.hostname != 'maimai.cn' or parsed.username or parsed.password:
        return '<span class="muted">暂无可用链接</span>'
    return (f'<a class="{esc(css)}" href="{esc(url)}" target="_blank" '
            f'rel="noopener noreferrer">{esc(label)} ↗</a>')


def apply_link(url):
    parsed = urlparse(str(url or ''))
    legacy = parsed.path == '/jobs/job_detail' and bool(parse_qs(parsed.query).get('ejid', [''])[0])
    public_page = bool(re.fullmatch(r'/n/job/geo/[A-Za-z0-9_-]+', parsed.path))
    if not (legacy or public_page):
        return '<span class="muted">暂无可用岗位入口</span>'
    return source_link(url, '查看岗位详情', 'apply-button')


def identity(item, kind):
    url = str(item.get('url') or '')
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    key = 'fid' if kind == 'post' else 'dstu'
    value = query.get(key, [''])[0]
    if not value and kind == 'post':
        match = re.search(r'/feed-detail/(\d+)', parsed.path)
        value = match.group(1) if match else ''
    return (parsed.hostname, key, value) if value else (url or json.dumps(item, sort_keys=True, ensure_ascii=False))


def unique(items, kind):
    output, seen = [], set()
    for item in items:
        key = identity(item, kind)
        if key not in seen:
            seen.add(key)
            output.append(item)
    return output


def prepare(data):
    groups = []
    if data.get('companies') is not None:
        if data.get('jobs'):
            raise ValueError('Use companies or legacy jobs, not both.')
        if not isinstance(data['companies'], list):
            raise ValueError('companies must be a list.')
        groups = [dict(c, jobs=list(c.get('jobs', []))) for c in data['companies']]
        keys = [(c.get('company_id') or c.get('company')) for c in groups]
        if len(keys) != len(set(keys)):
            raise ValueError('Merge duplicate company records before rendering.')
    else:
        jobs = data.get('jobs', [])
        if not isinstance(jobs, list):
            raise ValueError('jobs must be a list.')
        by_company = {}
        for index, job in enumerate(jobs):
            key = job.get('company_id') or job.get('company') or f'unknown-{index}'
            group = by_company.setdefault(key, dict(company=job.get('company'), jobs=[], referral_posts=[], employees=[]))
            group['jobs'].append(job)
            group['referral_posts'].extend(job.get('referral_posts', []))
            group['employees'].extend(job.get('employees', []))
            for status in ['referral_status', 'employees_status']:
                if job.get(status):
                    group.setdefault(status, job[status])
        groups = list(by_company.values())
    if len(groups) > 20 or sum(len(c['jobs']) for c in groups) > 50:
        raise ValueError('This renderer accepts at most 20 companies and 50 jobs.')
    for group in groups:
        group['referral_posts'] = unique(group.get('referral_posts', []), 'post')
        group['employees'] = unique(group.get('employees', []), 'person')
    groups.sort(key=lambda c: (not bool(c['referral_posts']), not has_people_result(c)))
    skills = data.get('skills')
    if skills is None:
        skills = [j['skills'] for c in groups for j in c['jobs'] if j.get('skills')]
    elif isinstance(skills, dict):
        skills = [skills]
    if not skills:
        skills = [dict(role_name='目标工种', items=[], status='尚无足够的已核验 JD 样本，暂不展示技能排名。')]
    distinct = {json.dumps(s, ensure_ascii=False, sort_keys=True): s for s in skills}
    return groups, list(distinct.values())


def render_post(post):
    excerpt = post.get('content_excerpt')
    body = f'<p class="source-text">{esc(excerpt)}</p>' if excerpt else (
        '<p class="muted">' + ('原文已核验，点击查看。' if post.get('content_verified') else '正文尚未核验') + '</p>')
    return (f'<li>{source_link(post.get("url"), post.get("title") or "内推原帖", "main-link")}{body}'
            f'<p class="byline">{esc(post.get("author"))} {esc(post.get("published_at"))}</p>'
            f'<p class="scope">{esc(post.get("scope") or "关联范围待确认")}</p>'
            f'{source_link(post.get("url"), "查看内推帖", "lead-button")}</li>')


def has_people_result(group):
    # Navigation is available without a verified person and is not a result.
    return bool(group.get('employees')) or group.get('has_company_people') is True


def company_people_url(company):
    keyword = str(company or '').strip()
    if not keyword:
        return None
    return ('https://maimai.cn/web/search_center?type=contact&query='
            + quote(keyword, safe='') + '&highlight=true')


def render_company_people(company):
    url = company_people_url(company)
    if not url:
        return '<li class="muted">公司名称未公开，暂无公司找人入口。</li>'
    return '<li>' + source_link(url, '去获取该公司人脉', 'lead-button people-search-button') + '</li>'


def render_job(job):
    facts = ' · '.join(str(job.get(k) or '待确认') for k in ['city', 'education', 'experience'])
    note = f'<p class="note">{esc(job["note"])}</p>' if job.get('note') else ''
    return (f'<article class="job-item"><div class="job-heading"><h4>{esc(job.get("title"))}</h4>'
            f'<span class="salary">{esc(job.get("salary"))}</span></div><p class="facts">{esc(facts)}</p>{note}'
            f'<div class="apply-row">{apply_link(job.get("detail_url"))}</div></article>')


def render_skills(skills):
    items = skills.get('items', [])[:10]
    sample_n = skills.get('sample_size', 0)
    if items and (not isinstance(sample_n, int) or isinstance(sample_n, bool) or sample_n < 2):
        raise ValueError('A skill ranking needs at least two verified JD samples.')
    rows = []
    for i, item in enumerate(items, 1):
        n = item.get('job_count')
        if not isinstance(n, int) or isinstance(n, bool) or not 0 < n <= sample_n:
            raise ValueError('Invalid skill sample count.')
        rows.append(f'<tr><td>{i:02d}</td><td>{esc(item.get("keyword"))}</td><td>{n}</td><td>{n/sample_n:.1%}</td></tr>')
    if rows:
        body = (f'<p class="muted">{esc(skills.get("scope"))} · 样本 {sample_n} 个岗位 · {esc(skills.get("snapshot_at"))}</p>'
                '<table><thead><tr><th>排名</th><th>专业技能</th><th>岗位数</th><th>占比</th></tr></thead>'
                f'<tbody>{"".join(rows)}</tbody></table>')
    else:
        body = f'<p class="muted">{esc(skills.get("status") or "样本不足，暂不展示技能排名。")}</p>'
    qualifier = '（本次样本）' if skills.get('sample_based', True) else ''
    return f'<section class="skills"><h2>{esc(skills.get("role_name") or "目标工种")}{qualifier}技能关键词 TOP10</h2>{body}</section>'


def render_fallback_button():
    return ('<button type="button" class="fallback-button" data-open-download-dialog '
            'aria-haspopup="dialog" aria-controls="maimai-download-dialog">'
            + esc(HR_FALLBACK_LABEL) + '</button>')


def render_download_dialog():
    qr = base64.b64encode((ROOT / 'assets/maimai-download-qr.png').read_bytes()).decode('ascii')
    return ('<dialog class="download-dialog" id="maimai-download-dialog" aria-labelledby="download-dialog-title">'
            '<button type="button" class="download-dialog-close" data-close-download-dialog aria-label="关闭二维码">×</button>'
            '<h2 id="download-dialog-title">扫码下载脉脉 App</h2>'
            f'<img class="download-qr" src="data:image/png;base64,{qr}" width="348" height="390" alt="脉脉 App 下载二维码">'
            '<p>打开脉脉，搜索“HR”，进入大厂急招HR专场。</p></dialog>')


def render(data):
    groups, skills = prepare(data)
    cards = []
    needs_download_dialog = not groups
    for index, group in enumerate(groups, 1):
        posts = ''.join(render_post(p) for p in group['referral_posts']) or f'<li class="muted">{esc(group.get("referral_status") or "本次未查询")}</li>'
        people = render_company_people(group.get('company'))
        jobs = ''.join(render_job(j) for j in group['jobs']) or f'<p class="muted">{esc(group.get("jobs_status") or "本次未查询关联岗位")}</p>'
        is_empty = not (group['referral_posts'] or has_people_result(group) or group['jobs'])
        needs_download_dialog = needs_download_dialog or is_empty
        empty_action = (
            '<div class="apply-row company-empty-action">'
            + render_fallback_button() + '</div>'
            if is_empty else ''
        )
        cards.append(f'''<article class="company-card">
<div class="company-heading"><span class="number">{index:02d}</span><h2>公司：{esc(group.get('company') or '名称未公开')}</h2></div>
<div class="lead-grid"><section class="referrals"><h3>相关内推帖子</h3><ul>{posts}</ul></section><section class="people"><h3>公司人脉 · 员工 / HR</h3><ul>{people}</ul></section></div>
<section class="associated-jobs"><h3>关联岗位</h3>{jobs}</section>{empty_action}</article>''')
    empty_state = (
        '<div class="empty"><p>本次尚未核验到符合条件的内推、人脉或关联岗位。</p>'
        + render_fallback_button() + '</div>'
    )
    values = dict(TITLE=esc(data.get('title') or '白领求职捷径'), CONTEXT=esc(data.get('context')),
                  CARDS='\n'.join(cards) or empty_state,
                  SKILLS='\n'.join(render_skills(s) for s in skills),
                  DOWNLOAD_DIALOG=render_download_dialog() if needs_download_dialog else '')
    template = (ROOT / 'assets/job-list.template.html').read_text(encoding='utf-8')
    return re.sub(r'\{\{(TITLE|CONTEXT|CARDS|SKILLS|DOWNLOAD_DIALOG)\}\}', lambda m: values[m.group(1)], template)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('response_json', type=Path)
    parser.add_argument('output_html', type=Path)
    args = parser.parse_args()
    data = json.loads(args.response_json.read_text(encoding='utf-8'))
    output = render(data)
    args.output_html.parent.mkdir(parents=True, exist_ok=True)
    args.output_html.write_text(output, encoding='utf-8')
    print(args.output_html)


if __name__ == '__main__':
    main()
