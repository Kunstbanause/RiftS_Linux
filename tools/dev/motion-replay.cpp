// Offline evaluation of the occlusion motion models (rift_s_motion.cpp, patch 0044) on RIFT_S_CTRL_RECORD captures.
//
// Replays both controllers through the fusion filter in arrival order, with the head pose from the
// optical recording, feeds the motion models at the IMU rate and compares the plain filter output
// ("filter") with the output the driver would publish with the models ("models"):
//
//   synthetic  optical fixes of one controller are cut out for --gap seconds in stretches that were
//              well tracked (windows every 5 s, hands alternating); the error against the cut-out
//              fixes is the ground truth comparison.
//   reacquire  real dropouts (> 0.3 s without an accepted fix): error at the first fix afterwards.
//   tracked    model active while the hand had an accepted fix in the last 50 ms (should be rare):
//              share of time and offset from the filter.
//
// Usage: motion-replay <recording dir> [--gap s] [--reliable cov=<m^2>|age=<ms>] [--per-window out.csv]
//        [--models list] (list of at_rest,near_head,hand_over_hand,behind_back,out_of_fov; default all)
#include "rift_s_controller_fusion.h"
#include "rift_s_motion.h"
#include "math/m_api.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <dirent.h>
#include <fstream>
#include <map>
#include <sstream>
#include <string>
#include <vector>

static const char *const MODEL_NAMES[] = {"none", "at_rest", "near_head", "hand_over_hand", "behind_back", "out_of_fov"};

struct Event
{
	int64_t arrival;
	int hand;
	bool is_imu;
	int64_t ts;
	float v[7];
};

struct HeadSample
{
	int64_t ts;
	xrt_pose pose;
};

struct Segment
{
	std::string name;
	int64_t t0, t1;
};

//! One output sample of a hand, at an IMU timestamp
struct Out
{
	int64_t ts;
	xrt_vec3 filter;
	xrt_vec3 models;
	int model;
	bool filter_tracked;
};

struct Window
{
	int hand;
	int64_t t0, t1;
};

struct Result
{
	std::vector<double> filter, models;
	std::map<int, int> model_count;
};

static std::vector<std::vector<std::string>>
read_csv(const std::string &path, std::vector<std::string> *header = nullptr)
{
	std::vector<std::vector<std::string>> rows;
	std::ifstream f(path);
	std::string line;
	std::getline(f, line);
	if (header) {
		std::stringstream hs(line);
		std::string c;
		while (std::getline(hs, c, ',')) {
			header->push_back(c);
		}
	}
	while (std::getline(f, line)) {
		std::vector<std::string> cols;
		std::stringstream ss(line);
		std::string c;
		while (std::getline(ss, c, ',')) {
			cols.push_back(c);
		}
		rows.push_back(cols);
	}
	return rows;
}

static double
dist(const xrt_vec3 &a, const xrt_vec3 &b)
{
	return std::sqrt((a.x - b.x) * (a.x - b.x) + (a.y - b.y) * (a.y - b.y) + (a.z - b.z) * (a.z - b.z));
}

static bool
head_at(const std::vector<HeadSample> &head, int64_t t, xrt_pose &out)
{
	if (head.empty() || t < head.front().ts || t > head.back().ts + 100000000) {
		return false;
	}
	auto it = std::lower_bound(head.begin(), head.end(), t, [](const HeadSample &h, int64_t v) { return h.ts < v; });
	if (it == head.end()) {
		out = head.back().pose;
		return true;
	}
	if (it == head.begin() || it->ts == t) {
		out = it->pose;
		return true;
	}
	const HeadSample &b = *it, &a = *(it - 1);
	if (b.ts - a.ts > 200000000) {
		return false; // no head pose recorded for a while
	}
	float alpha = (float)(t - a.ts) / (float)(b.ts - a.ts);
	math_quat_slerp(&a.pose.orientation, &b.pose.orientation, alpha, &out.orientation);
	out.position = {a.pose.position.x + alpha * (b.pose.position.x - a.pose.position.x),
	                a.pose.position.y + alpha * (b.pose.position.y - a.pose.position.y),
	                a.pose.position.z + alpha * (b.pose.position.z - a.pose.position.z)};
	return true;
}

struct Config
{
	bool use_models = true;
	unsigned model_mask = 0x3e; // bits 1..5
	bool reliable_cov = true;
	double reliable_cov_m2 = 0.001;
	double reliable_age_s = 0.1;
};

//! One replay. drops: per hand, optical fixes with ts inside a window are discarded.
struct Replay
{
	std::vector<Out> out[2];
	//! Accepted (or reset) fixes as the filter saw them: ts, position
	std::vector<std::pair<int64_t, xrt_vec3>> accepted[2];
	uint64_t imu_without_head = 0;
};

static Replay
run(const std::vector<Event> &ev, const std::vector<HeadSample> &head, const std::vector<Window> &drops, const Config &cfg)
{
	Replay r;
	rift_s_ctrl_fusion_params params;
	rift_s_ctrl_fusion_default_params(&params);
	rift_s_ctrl_fusion *f[2] = {rift_s_ctrl_fusion_create(&params), rift_s_ctrl_fusion_create(&params)};
	rift_s_motion *motion = rift_s_motion_create();
	bool was_init[2] = {false, false};

	for (const Event &e : ev) {
		if (!e.is_imu) {
			bool dropped = false;
			for (const Window &w : drops) {
				if (w.hand == e.hand && e.ts >= w.t0 && e.ts <= w.t1) {
					dropped = true;
					break;
				}
			}
			if (dropped) {
				continue;
			}
			xrt_pose p;
			p.position = {e.v[0], e.v[1], e.v[2]};
			p.orientation = {e.v[3], e.v[4], e.v[5], e.v[6]};
			auto res = rift_s_ctrl_fusion_pose(f[e.hand], e.ts, &p);
			if (res == RIFT_S_CTRL_FUSION_POSE_ACCEPTED || res == RIFT_S_CTRL_FUSION_POSE_RESET) {
				r.accepted[e.hand].push_back({e.ts, p.position});
			}
			continue;
		}

		xrt_vec3 g{e.v[0], e.v[1], e.v[2]};
		xrt_vec3 a{e.v[3], e.v[4], e.v[5]};
		rift_s_ctrl_fusion_imu(f[e.hand], e.ts, &g, &a);
		rift_s_ctrl_fusion_sample fs;
		bool init = rift_s_ctrl_fusion_get_newest_sample(f[e.hand], &fs);
		if (!init) {
			if (was_init[e.hand]) {
				rift_s_motion_reset_hand(motion, e.hand);
			}
			was_init[e.hand] = false;
			continue;
		}
		was_init[e.hand] = true;

		if (getenv("MR_DUMP_VAR")) {
			static FILE *vf = fopen(getenv("MR_DUMP_VAR"), "w");
			fprintf(vf, "%d,%lld,%lld,%g,%g,%g,%d\n", e.hand, (long long)fs.timestamp_ns,
			        (long long)(fs.timestamp_ns - fs.last_fix_ns), fs.position_variance[0], fs.position_variance[1],
			        fs.position_variance[2], fs.holding ? 1 : 0);
		}
		Out o{};
		o.ts = fs.timestamp_ns;
		o.filter = fs.pose.position;
		o.models = fs.pose.position;
		o.filter_tracked = !fs.holding;

		if (cfg.use_models) {
			rift_s_motion_sample ms{};
			ms.timestamp_ns = fs.timestamp_ns;
			ms.pose = fs.pose;
			ms.linear_velocity = fs.linear_velocity;
			ms.linear_acceleration = fs.linear_acceleration;
			ms.angular_velocity = fs.angular_velocity;
			double age_s = (double)(fs.timestamp_ns - fs.last_fix_ns) / 1e9;
			for (int i = 0; i < 3; i++) {
				if (fs.holding) {
					ms.position_variance[i] = -1;
				} else if (cfg.reliable_cov) {
					// The models call a position reliable below 0.001 m^2: scale so cov= sets that threshold
					ms.position_variance[i] = fs.position_variance[i] * (0.001 / cfg.reliable_cov_m2);
				} else {
					ms.position_variance[i] = age_s < cfg.reliable_age_s ? 1e-5 : 0.01;
				}
			}
			xrt_pose hp;
			if (head_at(head, fs.timestamp_ns, hp)) {
				rift_s_motion_update(motion, e.hand, &ms, &hp);
			} else {
				r.imu_without_head++;
			}
			xrt_space_relation rel{};
			auto model = rift_s_motion_get(motion, e.hand, fs.timestamp_ns, &rel);
			if (model != RIFT_S_MOTION_NONE && (cfg.model_mask & (1u << model))) {
				o.model = model;
				o.models = rel.pose.position;
			}
		}
		r.out[e.hand].push_back(o);
	}
	rift_s_motion_destroy(&motion);
	rift_s_ctrl_fusion_destroy(&f[0]);
	rift_s_ctrl_fusion_destroy(&f[1]);
	return r;
}

//! Output of the hand at or just before t
static const Out *
out_at(const std::vector<Out> &out, int64_t t)
{
	auto it = std::upper_bound(out.begin(), out.end(), t, [](int64_t v, const Out &o) { return v < o.ts; });
	if (it == out.begin()) {
		return nullptr;
	}
	return &*(it - 1);
}

static double
pct(std::vector<double> v, double p)
{
	if (v.empty()) {
		return NAN;
	}
	std::sort(v.begin(), v.end());
	return v[(size_t)(p * (v.size() - 1))];
}

static const char *
segment_of(const std::vector<Segment> &segs, int64_t t)
{
	for (const Segment &s : segs) {
		if (t >= s.t0 && t <= s.t1) {
			return s.name.c_str();
		}
	}
	return "other";
}

static void
print_errors(const char *label, std::map<std::string, Result> by_seg)
{
	Result all;
	for (const auto &kv : by_seg) {
		all.filter.insert(all.filter.end(), kv.second.filter.begin(), kv.second.filter.end());
		all.models.insert(all.models.end(), kv.second.models.begin(), kv.second.models.end());
		for (const auto &mc : kv.second.model_count) {
			all.model_count[mc.first] += mc.second;
		}
	}
	by_seg["~all"] = all;
	printf("%-10s %-8s %5s | %-26s | %-26s | models active\n", label, "segment", "n", "filter med / p90 / max cm",
	       "models med / p90 / max cm");
	for (const auto &kv : by_seg) {
		const Result &res = kv.second;
		std::string active;
		for (const auto &mc : res.model_count) {
			if (mc.first == 0) {
				continue;
			}
			char buf[64];
			snprintf(buf, sizeof(buf), "%s%s %d", active.empty() ? "" : ", ", MODEL_NAMES[mc.first], mc.second);
			active += buf;
		}
		auto mx = [](const std::vector<double> &v) { return v.empty() ? NAN : *std::max_element(v.begin(), v.end()); };
		printf("%-10s %-8s %5zu | %6.1f %6.1f %8.1f      | %6.1f %6.1f %8.1f      | %s\n", "", kv.first.c_str(),
		       res.filter.size(), 100 * pct(res.filter, 0.5), 100 * pct(res.filter, 0.9), 100 * mx(res.filter),
		       100 * pct(res.models, 0.5), 100 * pct(res.models, 0.9), 100 * mx(res.models), active.c_str());
	}
}

int
main(int argc, char **argv)
{
	if (argc < 2) {
		fprintf(stderr, "usage: %s <recording dir> [--gap s] [--reliable cov=m2|age=ms] [--models list] [--per-window out.csv]\n",
		        argv[0]);
		return 1;
	}
	std::string dir = argv[1];
	double gap_s = 0.6;
	Config cfg;
	const char *per_window_path = nullptr;
	for (int i = 2; i < argc; i++) {
		if (strcmp(argv[i], "--gap") == 0 && i + 1 < argc) {
			gap_s = atof(argv[++i]);
		} else if (strcmp(argv[i], "--reliable") == 0 && i + 1 < argc) {
			const char *v = argv[++i];
			if (strncmp(v, "cov=", 4) == 0) {
				cfg.reliable_cov = true;
				cfg.reliable_cov_m2 = atof(v + 4);
			} else if (strncmp(v, "age=", 4) == 0) {
				cfg.reliable_cov = false;
				cfg.reliable_age_s = atof(v + 4) / 1000.0;
			} else {
				fprintf(stderr, "bad --reliable %s\n", v);
				return 1;
			}
		} else if (strcmp(argv[i], "--models") == 0 && i + 1 < argc) {
			cfg.model_mask = 0;
			std::stringstream ss(argv[++i]);
			std::string name;
			while (std::getline(ss, name, ',')) {
				for (unsigned m = 1; m < 6; m++) {
					if (name == MODEL_NAMES[m]) {
						cfg.model_mask |= 1u << m;
					}
				}
			}
		} else if (strcmp(argv[i], "--per-window") == 0 && i + 1 < argc) {
			per_window_path = argv[++i];
		} else {
			fprintf(stderr, "unknown argument %s\n", argv[i]);
			return 1;
		}
	}

	// Files
	std::string imu_path[2], opt_path[2];
	DIR *d = opendir(dir.c_str());
	if (!d) {
		fprintf(stderr, "cannot open %s\n", dir.c_str());
		return 1;
	}
	while (dirent *de = readdir(d)) {
		std::string n = de->d_name;
		for (int h = 0; h < 2; h++) {
			std::string prefix = h == 0 ? "rift_s_left_" : "rift_s_right_";
			if (n.rfind(prefix, 0) == 0) {
				if (n.find("_imu.csv") != std::string::npos) {
					imu_path[h] = dir + "/" + n;
				} else if (n.find("_optical.csv") != std::string::npos) {
					opt_path[h] = dir + "/" + n;
				}
			}
		}
	}
	closedir(d);
	for (int h = 0; h < 2; h++) {
		if (imu_path[h].empty() || opt_path[h].empty()) {
			fprintf(stderr, "missing %s controller files in %s\n", h ? "right" : "left", dir.c_str());
			return 1;
		}
	}

	std::vector<Event> ev;
	std::vector<HeadSample> head;
	for (int h = 0; h < 2; h++) {
		for (auto &row : read_csv(imu_path[h])) {
			if (row.size() < 9) {
				continue;
			}
			Event e{};
			e.hand = h;
			e.is_imu = true;
			e.ts = atoll(row[0].c_str());
			e.arrival = atoll(row[2].c_str());
			for (int k = 0; k < 6; k++) {
				e.v[k] = (float)atof(row[3 + k].c_str());
			}
			ev.push_back(e);
		}
		std::vector<std::string> hdr;
		auto rows = read_csv(opt_path[h], &hdr);
		int head_col = -1;
		for (size_t c = 0; c < hdr.size(); c++) {
			if (hdr[c] == "head_px") {
				head_col = (int)c;
			}
		}
		if (head_col < 0) {
			fprintf(stderr, "%s has no head pose columns (recorded before patch 0012)\n", opt_path[h].c_str());
			return 1;
		}
		for (auto &row : rows) {
			if (row.size() < (size_t)head_col + 7) {
				continue;
			}
			Event e{};
			e.hand = h;
			e.is_imu = false;
			e.ts = atoll(row[0].c_str());
			e.arrival = atoll(row[1].c_str());
			for (int k = 0; k < 7; k++) {
				e.v[k] = (float)atof(row[2 + k].c_str());
			}
			ev.push_back(e);
			HeadSample hs;
			hs.ts = e.ts;
			hs.pose.position = {(float)atof(row[head_col].c_str()), (float)atof(row[head_col + 1].c_str()),
			                    (float)atof(row[head_col + 2].c_str())};
			hs.pose.orientation = {(float)atof(row[head_col + 3].c_str()), (float)atof(row[head_col + 4].c_str()),
			                       (float)atof(row[head_col + 5].c_str()), (float)atof(row[head_col + 6].c_str())};
			float n = hs.pose.orientation.x * hs.pose.orientation.x + hs.pose.orientation.y * hs.pose.orientation.y +
			          hs.pose.orientation.z * hs.pose.orientation.z + hs.pose.orientation.w * hs.pose.orientation.w;
			if (n > 0.5f) {
				head.push_back(hs);
			}
		}
	}
	std::stable_sort(ev.begin(), ev.end(), [](const Event &a, const Event &b) { return a.arrival < b.arrival; });
	std::sort(head.begin(), head.end(), [](const HeadSample &a, const HeadSample &b) { return a.ts < b.ts; });
	head.erase(std::unique(head.begin(), head.end(), [](const HeadSample &a, const HeadSample &b) { return a.ts == b.ts; }),
	           head.end());

	std::vector<Segment> segs;
	for (auto &row : read_csv(dir + "/segments.csv")) {
		if (row.size() >= 4) {
			segs.push_back({row[1], atoll(row[2].c_str()), atoll(row[3].c_str())});
		}
	}

	// Pass 1: the recording as it is
	Replay base = run(ev, head, {}, cfg);
	printf("%s: %zu head poses, IMU samples without a head pose %llu\n", dir.c_str(), head.size(),
	       (unsigned long long)base.imu_without_head);

	// Model activity and tracked-time displacement
	{
		printf("\nmodel activity (all IMU samples)\n");
		for (int h = 0; h < 2; h++) {
			std::map<int, int> count;
			size_t tracked = 0, tracked_with_model = 0;
			std::vector<double> offs;
			auto &acc = base.accepted[h];
			size_t k = 0;
			for (const Out &o : base.out[h]) {
				count[o.model]++;
				while (k + 1 < acc.size() && acc[k + 1].first <= o.ts) {
					k++;
				}
				bool recent = !acc.empty() && acc[k].first <= o.ts && o.ts - acc[k].first < 50000000;
				if (recent) {
					tracked++;
					if (o.model != 0) {
						tracked_with_model++;
						offs.push_back(dist(o.models, o.filter));
					}
				}
			}
			printf("  %s:", h ? "right" : "left ");
			for (auto &c : count) {
				printf(" %s %.1f%%", MODEL_NAMES[c.first], 100.0 * c.second / base.out[h].size());
			}
			printf("\n         model active while tracked (fix < 50 ms ago): %.2f%% of tracked time, offset from "
			       "filter med %.1f p90 %.1f max %.1f cm\n",
			       tracked ? 100.0 * tracked_with_model / tracked : 0.0, 100 * pct(offs, 0.5), 100 * pct(offs, 0.9),
			       offs.empty() ? NAN : 100 * *std::max_element(offs.begin(), offs.end()));
		}
	}

	// Output jumps (teleports): largest step between consecutive outputs
	{
		printf("\noutput steps between IMU samples (> 5 cm = teleport)\n");
		for (int h = 0; h < 2; h++) {
			double mf = 0, mm = 0;
			int jf = 0, jm = 0;
			for (size_t i = 1; i < base.out[h].size(); i++) {
				const Out &a = base.out[h][i - 1], &b = base.out[h][i];
				if (b.ts - a.ts > 20000000) {
					continue; // IMU gap
				}
				double df = dist(a.filter, b.filter), dm = dist(a.models, b.models);
				mf = std::max(mf, df);
				mm = std::max(mm, dm);
				jf += df > 0.05;
				jm += dm > 0.05;
			}
			printf("  %s: filter max %.1f cm (%d > 5 cm), models max %.1f cm (%d > 5 cm)\n", h ? "right" : "left ",
			       100 * mf, jf, 100 * mm, jm);
		}
	}

	// Real dropouts: error at the first accepted fix after > 0.3 s
	{
		std::map<std::string, Result> by_seg;
		for (int h = 0; h < 2; h++) {
			auto &acc = base.accepted[h];
			for (size_t i = 1; i < acc.size(); i++) {
				if (acc[i].first - acc[i - 1].first < 300000000) {
					continue;
				}
				// Output just before the fix was applied: the newest output before its exposure
				const Out *o = out_at(base.out[h], acc[i].first);
				if (!o) {
					continue;
				}
				Result &res = by_seg[segment_of(segs, acc[i].first)];
				res.filter.push_back(dist(o->filter, acc[i].second));
				res.models.push_back(dist(o->models, acc[i].second));
				res.model_count[o->model]++;
			}
		}
		printf("\nreal dropouts > 0.3 s: error at re-acquisition\n");
		print_errors("reacquire", by_seg);
	}

	// Synthetic occlusion
	{
		std::vector<Window> wins;
		int hand = 0;
		int64_t t_start = ev.front().arrival + 5000000000LL, t_end = ev.back().arrival - 2000000000LL;
		const int64_t gap_ns = (int64_t)(gap_s * 1e9);
		for (int64_t t = t_start; t + gap_ns < t_end; t += 5000000000LL) {
			// Find a start within the next 2 s where the hand is well tracked around the window
			auto &acc = base.accepted[hand];
			for (int64_t s = t; s < t + 2000000000LL; s += 100000000LL) {
				int64_t a0 = s - 500000000LL, a1 = s + gap_ns + 300000000LL;
				auto lo = std::lower_bound(acc.begin(), acc.end(), a0,
				                           [](const std::pair<int64_t, xrt_vec3> &x, int64_t v) { return x.first < v; });
				if (lo == acc.end() || lo->first - a0 > 100000000LL) {
					continue;
				}
				bool ok = true;
				int64_t prev = lo->first;
				for (auto it = lo + 1; it != acc.end() && it->first <= a1; ++it) {
					if (it->first - prev > 100000000LL) {
						ok = false;
						break;
					}
					prev = it->first;
				}
				if (ok && a1 - prev < 100000000LL) {
					wins.push_back({hand, s, s + gap_ns});
					break;
				}
			}
			hand = 1 - hand;
		}
		Replay cut = run(ev, head, wins, cfg);
		std::map<std::string, Result> by_seg;
		FILE *pw = per_window_path ? fopen(per_window_path, "w") : nullptr;
		if (pw) {
			fprintf(pw, "hand,t0_ns,segment,fixes,filter_mean_cm,models_mean_cm,filter_end_cm,models_end_cm,model\n");
		}
		for (const Window &w : wins) {
			std::vector<double> ef, em;
			std::map<int, int> mc;
			for (auto &fx : base.accepted[w.hand]) {
				if (fx.first < w.t0 || fx.first > w.t1) {
					continue;
				}
				const Out *o = out_at(cut.out[w.hand], fx.first);
				if (!o) {
					continue;
				}
				ef.push_back(dist(o->filter, fx.second));
				em.push_back(dist(o->models, fx.second));
				mc[o->model]++;
			}
			if (ef.empty()) {
				continue;
			}
			Result &res = by_seg[segment_of(segs, w.t0)];
			res.filter.insert(res.filter.end(), ef.begin(), ef.end());
			res.models.insert(res.models.end(), em.begin(), em.end());
			for (auto &m : mc) {
				res.model_count[m.first] += m.second;
			}
			if (pw) {
				double sf = 0, sm = 0;
				for (double v : ef) {
					sf += v;
				}
				for (double v : em) {
					sm += v;
				}
				int top = std::max_element(mc.begin(), mc.end(), [](auto &a, auto &b) { return a.second < b.second; })->first;
				fprintf(pw, "%d,%lld,%s,%zu,%.2f,%.2f,%.2f,%.2f,%s\n", w.hand, (long long)w.t0, segment_of(segs, w.t0),
				        ef.size(), 100 * sf / ef.size(), 100 * sm / em.size(), 100 * ef.back(), 100 * em.back(),
				        MODEL_NAMES[top]);
			}
		}
		if (pw) {
			fclose(pw);
		}
		printf("\nsynthetic occlusion: %zu windows of %.1f s, error against the cut-out fixes (counts are fixes)\n",
		       wins.size(), gap_s);
		print_errors("synthetic", by_seg);
	}
	return 0;
}
