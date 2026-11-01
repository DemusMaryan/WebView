// Thin RGB-only driver around the official ORB-SLAM3 tracking/mapping code.
#include "System.h"

#include <opencv2/imgcodecs.hpp>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <locale>
#include <map>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace fs = std::filesystem;
using Clock = std::chrono::steady_clock;

namespace {
struct InputFrame {
    double timestamp;
    fs::path image;
};

double Seconds(Clock::time_point begin, Clock::time_point end = Clock::now()) {
    return std::chrono::duration<double>(end - begin).count();
}

const char* StateName(int state) {
    switch (state) {
        case -1: return "SYSTEM_NOT_READY";
        case 0: return "NO_IMAGES_YET";
        case 1: return "NOT_INITIALIZED";
        case 2: return "OK";
        case 3: return "RECENTLY_LOST";
        case 4: return "LOST";
        case 5: return "OK_KLT";
        default: return "UNKNOWN";
    }
}

std::vector<InputFrame> LoadFrames(const fs::path& sequence) {
    std::ifstream input(sequence / "rgb.txt");
    if (!input)
        throw std::runtime_error("Cannot open RGB timestamp list: " + (sequence / "rgb.txt").string());
    std::vector<InputFrame> frames;
    std::string line;
    double previous = -std::numeric_limits<double>::infinity();
    while (std::getline(input, line)) {
        const auto first = line.find_first_not_of(" \t\r");
        if (first == std::string::npos || line[first] == '#')
            continue;
        std::istringstream row(line);
        double timestamp;
        std::string relative;
        if (!(row >> timestamp >> relative) || !std::isfinite(timestamp) || timestamp <= previous)
            throw std::runtime_error("Invalid or unordered RGB timestamp row");
        fs::path path(relative);
        if (path.is_absolute() || path.empty() || *path.begin() != "rgb")
            throw std::runtime_error("Only relative rgb/ image paths are accepted");
        for (const auto& component : path)
            if (component == "..")
                throw std::runtime_error("RGB path escapes the sequence directory");
        frames.push_back({timestamp, sequence / path});
        previous = timestamp;
    }
    if (frames.empty())
        throw std::runtime_error("RGB sequence is empty");
    return frames;
}

void MatrixJson(std::ostream& output, const Eigen::Matrix4f& matrix) {
    const auto precision = output.precision(9);
    output << '[';
    for (int row = 0; row < 4; ++row) {
        if (row) output << ',';
        output << '[';
        for (int column = 0; column < 4; ++column) {
            if (column) output << ',';
            output << matrix(row, column);
        }
        output << ']';
    }
    output << ']';
    output.precision(precision);
}

void PointsJson(std::ostream& output, const std::vector<ORB_SLAM3::ExportMapPoint>& points) {
    const auto precision = output.precision(9);
    output << '[';
    for (std::size_t index = 0; index < points.size(); ++index) {
        if (index) output << ',';
        const auto& point = points[index];
        output << "{\"id\":" << point.id << ",\"xyz\":["
               << point.xyz.x() << ',' << point.xyz.y() << ',' << point.xyz.z() << "]}";
    }
    output << ']';
    output.precision(precision);
}

void QuaternionJson(std::ostream& output, const Eigen::Matrix4f& matrix) {
    const Eigen::Quaternionf quaternion(Eigen::Matrix3f(matrix.block<3, 3>(0, 0)));
    const auto normalized = quaternion.normalized();
    const auto precision = output.precision(9);
    output << '[' << normalized.x() << ',' << normalized.y() << ','
           << normalized.z() << ',' << normalized.w() << ']';
    output.precision(precision);
}

std::string DigestHex(std::uint64_t digest) {
    std::ostringstream value;
    value << std::hex << std::setfill('0') << std::setw(16) << digest;
    return value.str();
}

void PointMetadataJson(std::ostream& output, const ORB_SLAM3::ExportSnapshot& snapshot) {
    output << "\"points_total\":" << snapshot.points_total
           << ",\"points_exported\":" << snapshot.points.size()
           << ",\"points_limit\":" << snapshot.points_limit
           << ",\"points_truncated\":" << (snapshot.points_total > snapshot.points.size() ? "true" : "false");
}

void ConsistencyJson(std::ostream& output, const ORB_SLAM3::ExportWorkerState& workers,
                     bool stable, const std::string& first, const std::string& second,
                     std::size_t comparisons) {
    output << "{\"workers_finished\":" << (workers.workers_finished ? "true" : "false")
           << ",\"mapper_started\":" << (workers.mapper_started ? "true" : "false")
           << ",\"loop_started\":" << (workers.loop_started ? "true" : "false")
           << ",\"mapper_finished\":" << (workers.mapper_finished ? "true" : "false")
           << ",\"loop_finished\":" << (workers.loop_finished ? "true" : "false")
           << ",\"active_gba\":" << workers.active_gba
           << ",\"geometry_stable\":" << (stable ? "true" : "false")
           << ",\"geometry_digest_first\":\"" << first
           << "\",\"geometry_digest_second\":\"" << second
           << "\",\"snapshot_comparison_count\":" << comparisons << '}';
}

void FailureSummary(const fs::path& directory, const char* reason,
                    const ORB_SLAM3::ExportWorkerState& workers, double waited,
                    const std::string& first = "", const std::string& second = "",
                    std::size_t comparisons = 0) {
    std::ofstream failure(directory / "native-summary.json");
    failure << std::setprecision(17) << "{\"success\":false,\"failure\":\"" << reason
            << "\",\"shutdown_wait_seconds\":" << waited << ",\"final_snapshot_consistency\":";
    ConsistencyJson(failure, workers, false, first, second, comparisons);
    failure << "}\n";
    failure.flush();
}

[[noreturn]] void ExitWithActiveWorkers(const fs::path& directory,
                                       const ORB_SLAM3::ExportWorkerState& workers,
                                       double waited) {
    FailureSummary(directory, "worker_completion_timeout", workers, waited);
    std::cerr << "[cv-export] completion wait did not establish quiescence (limit 60 seconds): mapper_finished="
              << workers.mapper_finished << " loop_finished=" << workers.loop_finished
              << " active_gba=" << workers.active_gba
              << "; terminating the process without destroying System while workers remain active.\n";
    std::cout.flush();
    std::cerr.flush();
    // Stop this standalone process and all its threads. No shutdown order is
    // changed, and no System destructor runs against still-active workers.
    std::_Exit(4);
}

void CheckOutput(const std::ofstream& output, const char* description) {
    if (!output)
        throw std::runtime_error(std::string("Cannot write ") + description);
}
} // namespace

int main(int argc, char** argv) {
    const auto total_begin = Clock::now();
    std::locale::global(std::locale::classic());
    if (argc < 5 || argc > 6) {
        std::cerr << "Usage: mono_export vocabulary settings sequence output_dir [max_frames]\n";
        return 2;
    }

    std::unique_ptr<ORB_SLAM3::System> slam;
    bool shutdown_requested = false;
    fs::path output_directory;
    try {
        const fs::path vocabulary = fs::absolute(argv[1]);
        const fs::path settings = fs::absolute(argv[2]);
        const fs::path sequence = fs::absolute(argv[3]);
        output_directory = fs::absolute(argv[4]);
        auto frames = LoadFrames(sequence);
        const auto available_frames = frames.size();
        if (argc == 6) {
            std::size_t parsed = 0;
            const std::string argument(argv[5]);
            if (argument.empty() || argument.front() == '-')
                throw std::runtime_error("max_frames must be a positive integer");
            const auto maximum = std::stoull(argument, &parsed);
            if (parsed != argument.size() || maximum == 0)
                throw std::runtime_error("max_frames must be a positive integer");
            frames.resize(std::min<std::size_t>(frames.size(), maximum));
        }
        fs::create_directories(output_directory);
        for (const auto* name : {"snapshots.jsonl", "trajectory.json", "map_points.json", "native-summary.json"})
            if (fs::exists(output_directory / name))
                throw std::runtime_error("Refusing to overwrite an existing native result: " + (output_directory / name).string());

        cv::FileStorage calibration(settings.string(), cv::FileStorage::READ);
        if (!calibration.isOpened() || calibration["Camera.RGB"].empty() ||
            static_cast<int>(calibration["Camera.RGB"]) != 0)
            throw std::runtime_error("Settings must declare Camera.RGB: 0 for OpenCV BGR decoding");
        const int width = static_cast<int>(calibration["Camera.width"]);
        const int height = static_cast<int>(calibration["Camera.height"]);
        if (width <= 0 || height <= 0)
            throw std::runtime_error("Settings must declare the calibrated camera width and height");
        calibration.release();

        std::ofstream snapshots(output_directory / "snapshots.jsonl");
        snapshots << std::setprecision(17);
        CheckOutput(snapshots, "snapshots.jsonl");
        const auto startup_begin = Clock::now();
        slam = std::make_unique<ORB_SLAM3::System>(
            vocabulary.string(), settings.string(), ORB_SLAM3::System::MONOCULAR, false);
        const double startup_seconds = Seconds(startup_begin);
        if (std::abs(slam->GetImageScale() - 1.0f) > 1e-6f)
            throw std::runtime_error("This prototype requires the original calibrated image size; no resizing is performed");

        std::size_t processed_frames = 0;
        std::size_t valid_poses = 0;
        std::map<int, std::size_t> state_counts;
        std::map<std::string, std::size_t> unavailable_counts;
        struct LiveFrame {
            std::size_t ordinal;
            std::int64_t map_id;
            std::size_t segment_id;
            bool pose_valid;
        };
        std::map<double, LiveFrame> live_frames;
        bool have_previous_map = false;
        std::int64_t previous_map = -1;
        std::size_t segment_id = 0;
        double track_seconds = 0.0;
        const auto processing_begin = Clock::now();
        for (std::size_t frame_id = 0; frame_id < frames.size(); ++frame_id) {
            const auto& frame = frames[frame_id];
            const cv::Mat image = cv::imread(frame.image.string(), cv::IMREAD_COLOR);
            if (image.empty() || image.cols != width || image.rows != height)
                throw std::runtime_error("Missing image or calibrated resolution mismatch: " + frame.image.string());
            const auto track_begin = Clock::now();
            slam->TrackMonocular(image, frame.timestamp);
            const double frame_track_seconds = Seconds(track_begin);
            track_seconds += frame_track_seconds;
            const auto snapshot = slam->GetMonocularSnapshot(2000, false);
            if (have_previous_map && snapshot.map_id != previous_map)
                ++segment_id;
            previous_map = snapshot.map_id;
            have_previous_map = true;
            ++processed_frames;
            ++state_counts[snapshot.tracking_state];
            if (snapshot.pose_valid) ++valid_poses;
            else ++unavailable_counts[snapshot.pose_unavailable_reason];
            live_frames.emplace(frame.timestamp, LiveFrame{frame_id, snapshot.map_id, segment_id, snapshot.pose_valid});

            snapshots << "{\"type\":\"frame\",\"frame_id\":" << frame_id
                      << ",\"ordinal\":" << frame_id
                      << ",\"timestamp\":" << frame.timestamp
                      << ",\"processed_frames\":" << processed_frames
                      << ",\"tracking_state\":" << snapshot.tracking_state
                      << ",\"tracking_state_name\":\"" << StateName(snapshot.tracking_state)
                      << "\",\"pose_valid\":" << (snapshot.pose_valid ? "true" : "false")
                      << ",\"Twc\":";
            if (snapshot.pose_valid) MatrixJson(snapshots, snapshot.Twc);
            else snapshots << "null";
            snapshots << ",\"quaternion_xyzw\":";
            if (snapshot.pose_valid) QuaternionJson(snapshots, snapshot.Twc);
            else snapshots << "null";
            snapshots << ",\"pose_unavailable_reason\":";
            if (snapshot.pose_valid) snapshots << "null";
            else snapshots << '"' << snapshot.pose_unavailable_reason << '"';
            snapshots << ",\"map_id\":" << snapshot.map_id
                      << ",\"segment_id\":" << segment_id
                      << ",\"map_change_index\":" << snapshot.map_change_index
                      << ',';
            PointMetadataJson(snapshots, snapshot);
            snapshots << ",\"points\":";
            PointsJson(snapshots, snapshot.points);
            snapshots << ",\"track_seconds\":" << frame_track_seconds
                      << ",\"elapsed_processing_seconds\":" << Seconds(processing_begin) << "}\n";
            snapshots.flush();
            CheckOutput(snapshots, "snapshots.jsonl");
            if (processed_frames == 1 || processed_frames % 30 == 0 || processed_frames == frames.size())
                std::cout << "[cv-export] frame=" << frame_id << std::endl;
        }
        const double processing_seconds = Seconds(processing_begin);
        snapshots.close();
        const auto shutdown_begin = Clock::now();
        shutdown_requested = true;
        slam->Shutdown(); // Upstream behavior: this requests finish, without joining workers.
        const double shutdown_seconds = Seconds(shutdown_begin);

        const auto wait_begin = Clock::now();
        auto workers = slam->WaitForExportQuiescence(60000);
        const double shutdown_wait_seconds = Seconds(wait_begin);
        if (!workers.workers_finished)
            ExitWithActiveWorkers(output_directory, workers, shutdown_wait_seconds);
        const auto snapshot_begin = Clock::now();
        const auto first_final_snapshot = slam->GetMonocularSnapshot(100000, true);
        const auto final_snapshot = slam->GetMonocularSnapshot(100000, true);
        workers = slam->ObserveExportWorkers();
        const double final_snapshot_seconds = Seconds(snapshot_begin);
        const bool geometry_stable = workers.workers_finished && final_snapshot.map_id >= 0 &&
            !first_final_snapshot.geometry_words.empty() &&
            first_final_snapshot.geometry_words == final_snapshot.geometry_words &&
            first_final_snapshot.geometry_digest == final_snapshot.geometry_digest;
        const auto first_digest = DigestHex(first_final_snapshot.geometry_digest);
        const auto second_digest = DigestHex(final_snapshot.geometry_digest);
        if (!geometry_stable) {
            FailureSummary(output_directory, "final_geometry_not_stable", workers, shutdown_wait_seconds,
                           first_digest, second_digest, 1);
            std::cerr << "[cv-export] final geometry comparison failed: " << first_digest << " versus " << second_digest << '\n';
            if (!workers.workers_finished)
                ExitWithActiveWorkers(output_directory, workers, shutdown_wait_seconds);
            return 5;
        }
        // Keep the final observed segment only; map merges are not silently
        // treated as proof that earlier segments share a coordinate frame.
        std::int64_t final_segment = -1;
        for (const auto& entry : live_frames)
            if (entry.second.map_id == final_snapshot.map_id)
                final_segment = std::max(final_segment, static_cast<std::int64_t>(entry.second.segment_id));
        struct FinalPose { LiveFrame input; double timestamp; Eigen::Matrix4f Twc; };
        std::map<std::size_t, FinalPose> final_poses_by_ordinal;
        std::size_t omitted_live_invalid = 0, omitted_map_or_segment = 0, omitted_duplicates = 0;
        for (const auto& pose : final_snapshot.trajectory) {
            const auto input = live_frames.find(pose.timestamp);
            if (input == live_frames.end() || !input->second.pose_valid) { ++omitted_live_invalid; continue; }
            if (input->second.map_id != final_snapshot.map_id ||
                static_cast<std::int64_t>(input->second.segment_id) != final_segment) { ++omitted_map_or_segment; continue; }
            if (!final_poses_by_ordinal.emplace(input->second.ordinal, FinalPose{input->second, pose.timestamp, pose.Twc}).second)
                ++omitted_duplicates;
        }
        std::ofstream trajectory(output_directory / "trajectory.json");
        trajectory << std::setprecision(17)
                   << "{\"map_id\":" << final_snapshot.map_id
                   << ",\"segment_id\":" << final_segment
                   << ",\"coordinate_convention\":\"Twc\",\"scale\":\"relative\",\"poses\":[";
        std::size_t final_poses = 0;
        for (const auto& entry : final_poses_by_ordinal) {
            const auto& pose = entry.second;
            if (final_poses++) trajectory << ',';
            trajectory << "{\"type\":\"pose\",\"frame_id\":" << pose.input.ordinal
                       << ",\"ordinal\":" << pose.input.ordinal << ",\"timestamp\":" << pose.timestamp
                       << ",\"map_id\":" << pose.input.map_id << ",\"segment_id\":" << pose.input.segment_id
                       << ",\"Twc\":";
            MatrixJson(trajectory, pose.Twc);
            trajectory << ",\"quaternion_xyzw\":";
            QuaternionJson(trajectory, pose.Twc);
            trajectory << '}';
        }
        trajectory << "]}\n";
        trajectory.flush();
        CheckOutput(trajectory, "trajectory.json");

        std::ofstream points(output_directory / "map_points.json");
        points << "{\"map_id\":" << final_snapshot.map_id
               << ",\"segment_id\":" << final_segment << ',';
        PointMetadataJson(points, final_snapshot);
        points << ",\"points\":";
        PointsJson(points, final_snapshot.points);
        points << "}\n";
        points.flush();
        CheckOutput(points, "map_points.json");

        std::ofstream summary(output_directory / "native-summary.json");
        summary << std::setprecision(17)
                << "{\"processed_frames\":" << processed_frames
                << ",\"frames_available\":" << available_frames
                << ",\"valid_poses\":" << valid_poses
                << ",\"invalid_poses\":" << processed_frames - valid_poses
                << ",\"final_poses\":" << final_poses
                << ",\"final_points\":" << final_snapshot.points.size()
                << ',';
        PointMetadataJson(summary, final_snapshot);
        summary
                << ",\"snapshots\":" << processed_frames
                << ",\"map_id\":" << final_snapshot.map_id
                << ",\"segment_id\":" << final_segment
                << ",\"ok_tracking_frames\":" << state_counts[2]
                << ",\"unavailable_ok_snapshot_poses\":" << state_counts[2] - valid_poses
                << ",\"retained_numeric_records\":" << final_snapshot.retained_numeric_records
                << ",\"resolved_numeric_records\":" << final_snapshot.trajectory.size()
                << ",\"omitted_reference_poses\":" << final_snapshot.omitted_reference_poses
                << ",\"omitted_other_map_poses\":" << final_snapshot.omitted_other_map_poses
                << ",\"omitted_nonfinite_poses\":" << final_snapshot.omitted_nonfinite_poses
                << ",\"omitted_live_invalid_poses\":" << omitted_live_invalid
                << ",\"omitted_map_or_segment_poses\":" << omitted_map_or_segment
                << ",\"omitted_duplicate_poses\":" << omitted_duplicates
                << ",\"published_keyframes\":" << final_snapshot.published_keyframes
                << ",\"tracking_state_counts\":{";
        bool first_state = true;
        for (const auto& [state, count] : state_counts) {
            if (!first_state) summary << ',';
            first_state = false;
            summary << '"' << StateName(state) << "\":" << count;
        }
        summary << "},\"pose_unavailable_reason_counts\":{";
        bool first_reason = true;
        for (const auto& entry : unavailable_counts) {
            if (!first_reason) summary << ',';
            first_reason = false;
            summary << '"' << entry.first << "\":" << entry.second;
        }
        summary << "},\"startup_seconds\":" << startup_seconds
                << ",\"processing_seconds\":" << processing_seconds
                << ",\"track_seconds\":" << track_seconds
                << ",\"shutdown_seconds\":" << shutdown_seconds
                << ",\"shutdown_wait_seconds\":" << shutdown_wait_seconds
                << ",\"final_snapshot_seconds\":" << final_snapshot_seconds
                << ",\"total_seconds\":" << Seconds(total_begin)
                << ",\"processing_fps\":" << processed_frames / processing_seconds
                << ",\"coordinate_convention\":\"Twc\",\"scale\":\"relative\""
                << ",\"live_points_cap\":2000,\"final_points_cap\":100000"
                << ",\"viewer_enabled\":false,\"pacing_enabled\":false"
                << ",\"background_threads_joined\":false"
                << ",\"final_snapshot_kind\":\"quiescent_workers_exact_geometry_comparison\""
                << ",\"final_snapshot_consistency\":";
        ConsistencyJson(summary, workers, geometry_stable, first_digest, second_digest, 1);
        summary << "}\n";
        summary.flush();
        CheckOutput(summary, "native-summary.json");
        if (!valid_poses || !final_poses || final_snapshot.points.empty()) {
            std::cerr << "SLAM result is empty or never initialized; inspect the native summary and logs.\n";
            return 3;
        }
        std::cout << "[cv-export] complete frames=" << processed_frames
                  << " poses=" << final_poses << " points=" << final_snapshot.points.size() << std::endl;
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "[cv-export] error: " << error.what() << '\n';
        if (slam) {
            if (!shutdown_requested) slam->Shutdown();
            const auto wait_begin = Clock::now();
            const auto workers = slam->WaitForExportQuiescence(60000);
            if (!workers.workers_finished)
                ExitWithActiveWorkers(output_directory, workers, Seconds(wait_begin));
        }
        return 2;
    }
}
