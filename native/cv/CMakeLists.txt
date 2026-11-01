cmake_minimum_required(VERSION 3.16)
project(MonoTrajectoryCV LANGUAGES CXX)

set(ORB_ROOT "" CACHE PATH "Pinned ORB-SLAM3 checkout on the native WSL filesystem")
if(NOT EXISTS "${ORB_ROOT}/include/System.h")
    message(FATAL_ERROR "Set ORB_ROOT to the prepared ORB-SLAM3 checkout")
endif()

find_package(OpenCV 4 REQUIRED)
find_package(Eigen3 3.1 REQUIRED NO_MODULE)
find_package(Pangolin REQUIRED)
find_package(Threads REQUIRED)

add_executable(mono_export mono_export.cc)
target_compile_features(mono_export PRIVATE cxx_std_17)
# The pinned core exposes Eigen types and is built with -march=native. Match
# its Eigen alignment/struct ABI; default SSE alignment is incompatible here.
target_compile_options(mono_export PRIVATE -march=native)
set_target_properties(mono_export PROPERTIES CXX_EXTENSIONS OFF)
target_include_directories(mono_export SYSTEM PRIVATE
    "${ORB_ROOT}"
    "${ORB_ROOT}/include"
    "${ORB_ROOT}/include/CameraModels"
    "${ORB_ROOT}/Thirdparty/Sophus"
    ${OpenCV_INCLUDE_DIRS}
    ${Pangolin_INCLUDE_DIRS})
target_link_libraries(mono_export PRIVATE
    "${ORB_ROOT}/lib/libORB_SLAM3.so"
    "${ORB_ROOT}/Thirdparty/DBoW2/lib/libDBoW2.so"
    "${ORB_ROOT}/Thirdparty/g2o/lib/libg2o.so"
    ${OpenCV_LIBS}
    ${Pangolin_LIBRARIES}
    Eigen3::Eigen
    Threads::Threads)

set(runtime_paths
    "${ORB_ROOT}/lib"
    "${ORB_ROOT}/Thirdparty/DBoW2/lib"
    "${ORB_ROOT}/Thirdparty/g2o/lib")
foreach(prefix IN LISTS CMAKE_PREFIX_PATH)
    list(APPEND runtime_paths "${prefix}/lib")
endforeach()
set_target_properties(mono_export PROPERTIES BUILD_RPATH "${runtime_paths}")
