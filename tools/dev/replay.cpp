// Offline replay of RIFT_S_CTRL_RECORD captures through the controller fusion filter.
//
// Feeds IMU samples and optical fixes in arrival order, like the driver sees them,
// and reports accept / reject / reset counts, innovation statistics and output
// smoothness. Parameters can be overridden as key=value arguments.
//
// Usage: replay <imu.csv> <optical.csv> [key=value ...] [lever=x,y,z] [--trace trace.csv] [--out output.csv]
// --out writes the filter output queried at 80 Hz (10 ms ahead), like a compositor would.
// lever= moves every optical fix by x,y,z metres in the fix's own (IMU) frame.
#include "rift_s_controller_fusion.h"
#include "math/m_api.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

struct Event
{
	int64_t arrival;
	bool is_imu;
	int64_t ts;
	float v[7];
};

static std::vector<std::vector<std::string>>
read_csv(const char *path)
{
	std::vector<std::vector<std::string>> rows;
	std::ifstream f(path);
	std::string line;
	std::getline(f, line); // header
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

static bool
set_param(rift_s_ctrl_fusion_params &p, const char *kv, int64_t &imu_offset_ns)
{
	const char *eq = strchr(kv, '=');
	if (eq == nullptr) {
		return false;
	}
	std::string k(kv, eq - kv);
	double v = atof(eq + 1);
#define P(name)                                                                                                        \
	if (k == #name) {                                                                                              \
		p.name = (decltype(p.name))v;                                                                          \
		return true;                                                                                           \
	}
	P(gyro_noise)
	P(accel_noise)
	P(gyro_bias_noise)
	P(accel_bias_noise)
	P(optical_pos_sigma)
	P(optical_rot_sigma)
	P(gate_chi2)
	P(reset_after_rejects)
	P(reset_consistency_pos)
	P(reset_consistency_rot)
	P(reset_force_after)
	P(reset_rot_max)
	P(reset_trust_time)
	P(coast_start)
	P(coast_vel_tau)
	P(coast_accel_noise)
	P(coast_max)
	P(hold_pos_noise)
	P(imu_gap_max)
	P(prediction_max)
	P(predict_with_accel)
#undef P
	if (k == "imu_offset_ms") {
		imu_offset_ns = (int64_t)(v * 1e6);
		return true;
	}
	return false;
}

int
main(int argc, char **argv)
{
	if (argc < 3) {
		fprintf(stderr, "usage: %s imu.csv optical.csv [key=value ...] [--trace out.csv]\n", argv[0]);
		return 1;
	}
	rift_s_ctrl_fusion_params params;
	rift_s_ctrl_fusion_default_params(&params);
	int64_t imu_offset_ns = 0;
	double skip_s = 0;
	double accel_scale = 1.0;
	xrt_vec3 lever = {0, 0, 0}; // shift applied to each optical fix in its own body frame
	const char *trace_path = nullptr;
	const char *out_path = nullptr;
	for (int i = 3; i < argc; i++) {
		if (strcmp(argv[i], "--trace") == 0 && i + 1 < argc) {
			trace_path = argv[++i];
		} else if (strcmp(argv[i], "--out") == 0 && i + 1 < argc) {
			out_path = argv[++i];
		} else if (strncmp(argv[i], "accel_scale=", 12) == 0) {
			accel_scale = atof(argv[i] + 12);
		} else if (strncmp(argv[i], "lever=", 6) == 0) {
			sscanf(argv[i] + 6, "%f,%f,%f", &lever.x, &lever.y, &lever.z);
		} else if (strncmp(argv[i], "skip_s=", 7) == 0) {
			skip_s = atof(argv[i] + 7);
		} else if (!set_param(params, argv[i], imu_offset_ns)) {
			fprintf(stderr, "unknown argument %s\n", argv[i]);
			return 1;
		}
	}

	std::vector<Event> ev;
	for (auto &r : read_csv(argv[1])) {
		if (r.size() < 9) {
			continue;
		}
		Event e{};
		e.is_imu = true;
		e.ts = atoll(r[0].c_str()) + imu_offset_ns;
		e.arrival = atoll(r[2].c_str());
		for (int k = 0; k < 6; k++) {
			e.v[k] = (float)(atof(r[3 + k].c_str()) * (k >= 3 ? accel_scale : 1.0));
		}
		ev.push_back(e);
	}
	for (auto &r : read_csv(argv[2])) {
		if (r.size() < 10) {
			continue;
		}
		Event e{};
		e.is_imu = false;
		e.ts = atoll(r[0].c_str());
		e.arrival = atoll(r[1].c_str());
		for (int k = 0; k < 7; k++) {
			e.v[k] = (float)atof(r[2 + k].c_str());
		}
		ev.push_back(e);
	}
	std::stable_sort(ev.begin(), ev.end(), [](const Event &a, const Event &b) { return a.arrival < b.arrival; });
	if (skip_s > 0 && !ev.empty()) {
		int64_t cut = ev.front().arrival + (int64_t)(skip_s * 1e9);
		ev.erase(ev.begin(), std::find_if(ev.begin(), ev.end(), [cut](const Event &e) { return e.arrival >= cut; }));
	}

	rift_s_ctrl_fusion *f = rift_s_ctrl_fusion_create(&params);
	FILE *trace = trace_path ? fopen(trace_path, "w") : nullptr;
	FILE *out = out_path ? fopen(out_path, "w") : nullptr;
	if (out) {
		fprintf(out, "t_ns,tracked,px,py,pz,qx,qy,qz,qw\n");
	}
	if (trace) {
		fprintf(trace, "frame_ns,arrival_ns,result,chi2,prior_pos_err,prior_rot_err_deg,fix_x,fix_y,fix_z,prior_x,prior_y,prior_z\n");
	}

	int counts[4] = {0, 0, 0, 0};
	std::vector<double> chi2s, innovs;
	// Output smoothness: query at each IMU arrival + 10 ms like a compositor would
	double sum_step_sq = 0;
	int n_steps = 0;
	double max_step = 0;
	bool have_prev = false;
	float prev[3] = {0, 0, 0};
	int64_t next_query = 0;

	for (auto &e : ev) {
		if (e.is_imu) {
			xrt_vec3 g{e.v[0], e.v[1], e.v[2]};
			xrt_vec3 a{e.v[3], e.v[4], e.v[5]};
			rift_s_ctrl_fusion_imu(f, e.ts, &g, &a);
		} else {
			xrt_pose p;
			p.position = {e.v[0], e.v[1], e.v[2]};
			p.orientation = {e.v[3], e.v[4], e.v[5], e.v[6]};
			xrt_vec3 off;
			math_quat_rotate_vec3(&p.orientation, &lever, &off);
			p.position = {p.position.x + off.x, p.position.y + off.y, p.position.z + off.z};
			xrt_space_relation prior{};
			bool have_prior = rift_s_ctrl_fusion_get_relation(f, e.ts, &prior);
			auto res = rift_s_ctrl_fusion_pose(f, e.ts, &p);
			counts[res]++;
			rift_s_ctrl_fusion_stats st;
			rift_s_ctrl_fusion_get_stats(f, &st);
			if (res == RIFT_S_CTRL_FUSION_POSE_ACCEPTED || res == RIFT_S_CTRL_FUSION_POSE_REJECTED) {
				chi2s.push_back(st.last_chi2);
				innovs.push_back(st.last_pos_innovation);
			}
			if (trace) {
				// Every fix; prior errors are -1 while the filter has no prior
				double pe = -1, re = -1;
				if (have_prior) {
					pe = std::sqrt(std::pow(p.position.x - prior.pose.position.x, 2) +
					               std::pow(p.position.y - prior.pose.position.y, 2) +
					               std::pow(p.position.z - prior.pose.position.z, 2));
					double dot = std::fabs(p.orientation.x * prior.pose.orientation.x +
					                       p.orientation.y * prior.pose.orientation.y +
					                       p.orientation.z * prior.pose.orientation.z +
					                       p.orientation.w * prior.pose.orientation.w);
					re = 2 * std::acos(std::min(1.0, dot)) * 180.0 / M_PI;
				}
				fprintf(trace, "%lld,%lld,%d,%f,%f,%f,%f,%f,%f,%f,%f,%f\n", (long long)e.ts, (long long)e.arrival, (int)res,
				        st.last_chi2, pe, re, p.position.x, p.position.y, p.position.z, prior.pose.position.x,
				        prior.pose.position.y, prior.pose.position.z);
			}
		}
		if (e.arrival >= next_query) {
			next_query = e.arrival + 12500000; // 80 Hz
			xrt_space_relation rel{};
			bool have_rel = rift_s_ctrl_fusion_get_relation(f, e.arrival + 10000000, &rel);
			if (out && have_rel) {
				const xrt_pose &o = rel.pose;
				fprintf(out, "%lld,%d,%f,%f,%f,%f,%f,%f,%f\n", (long long)(e.arrival + 10000000),
				        (rel.relation_flags & XRT_SPACE_RELATION_POSITION_TRACKED_BIT) ? 1 : 0, o.position.x,
				        o.position.y, o.position.z, o.orientation.x, o.orientation.y, o.orientation.z,
				        o.orientation.w);
			}
			if (have_rel && (rel.relation_flags & XRT_SPACE_RELATION_POSITION_TRACKED_BIT)) {
				float *q = &rel.pose.position.x;
				if (have_prev) {
					double d = std::sqrt(std::pow(q[0] - prev[0], 2) + std::pow(q[1] - prev[1], 2) +
					                     std::pow(q[2] - prev[2], 2));
					sum_step_sq += d * d;
					n_steps++;
					max_step = std::max(max_step, d);
				}
				memcpy(prev, q, sizeof(prev));
				have_prev = true;
			} else {
				have_prev = false;
			}
		}
	}

	auto pct = [](std::vector<double> v, double p) {
		if (v.empty()) {
			return 0.0;
		}
		std::sort(v.begin(), v.end());
		return v[(size_t)(p * (v.size() - 1))];
	};
	rift_s_ctrl_fusion_stats st;
	rift_s_ctrl_fusion_get_stats(f, &st);
	int total = counts[0] + counts[1] + counts[2] + counts[3];
	printf("fixes %d: accepted %.1f%% rejected %.1f%% reset %.1f%% ignored %.1f%% | chi2 med %.1f p90 %.1f | "
	       "innov med %.1f mm p90 %.1f mm | out step rms %.1f mm max %.0f mm | bg %.3f %.3f %.3f ba %.2f %.2f %.2f\n",
	       total, 100.0 * counts[0] / total, 100.0 * counts[1] / total, 100.0 * counts[2] / total,
	       100.0 * counts[3] / total, pct(chi2s, 0.5), pct(chi2s, 0.9), 1000 * pct(innovs, 0.5),
	       1000 * pct(innovs, 0.9), n_steps ? 1000 * std::sqrt(sum_step_sq / n_steps) : 0.0, 1000 * max_step,
	       st.gyro_bias.x, st.gyro_bias.y, st.gyro_bias.z, st.accel_bias.x, st.accel_bias.y, st.accel_bias.z);
	if (trace) {
		fclose(trace);
	}
	if (out) {
		fclose(out);
	}
	rift_s_ctrl_fusion_destroy(&f);
	return 0;
}
