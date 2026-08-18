__global__ void kernel() {}
__device__ int helper(int x) { return x; }
int main() { helper(1); return 0; }
