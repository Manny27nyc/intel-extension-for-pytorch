#include <ATen/ATen.h>

#include <ATen/NativeFunctions.h>
#include <ATen/Parallel.h>
#include <ATen/native/ReduceOpsUtils.h>
#include <ATen/native/cpu/utils.h>
#include <ATen/record_function.h>
#include <c10/util/irange.h>

#include "Sum.h"
#include "csrc/utils/ipex_op_profile.h"
#include "csrc/utils/library.h"

namespace torch_ipex {
namespace cpu {

DEFINE_DISPATCH(sum_kernel_stub);

at::Tensor sum_out_cpu(
    const at::Tensor& input,
    c10::IntArrayRef dim,
    bool keepdim,
    c10::optional<c10::ScalarType> dtype) {
  auto input_ = input.is_contiguous() ? input : input.contiguous();
#if defined(IPEX_DISP_OP)
  printf("torch_ipex::sum_out_cpu\n");
#endif
  IPEX_RECORD_FUNCTION("torch_ipex::sum_out_cpu", std::vector<c10::IValue>({}));

  // if dtype does not have value, we assign the input type to dtype
  // if dtype is an integer, we promote it to kLong.
  if (!dtype.has_value()) {
    dtype = input_.scalar_type();
    if (at::isIntegralType(dtype.value(), /*includeBool=*/true)) {
      dtype = kLong;
    }
  }
  at::DimVector dims_(dim);
  at::maybe_wrap_dims(dims_, input_.dim());
  auto shape = at::meta::get_reduction_shape(input_, dims_, keepdim);
  at::Tensor output = at::empty(shape, input_.options().dtype(dtype));

  auto iter = at::meta::make_reduction_from_out_ty(
      input_, output, dim, keepdim, output.scalar_type());
  if (iter.numel() == 0) {
    output.zero_();
  } else {
#if defined(DYN_DISP_BUILD)
    sum_kernel_stub(kCPU, iter);
#else
    sum_kernel_impl(iter);
#endif
  }

  return output;
}

IPEX_TORCH_LIBRARY_IMPL(aten, CPU, m) {
  m.impl(
      TORCH_SELECTIVE_NAME("aten::sum.dim_IntList"),
      TORCH_FN((&torch_ipex::cpu::sum_out_cpu)));
}

} // namespace cpu
} // namespace torch_ipex
