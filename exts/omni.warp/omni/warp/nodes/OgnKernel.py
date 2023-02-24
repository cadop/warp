# Copyright (c) 2023 NVIDIA CORPORATION.  All rights reserved.
# NVIDIA CORPORATION and its licensors retain all intellectual property
# and proprietary rights in and to this software, related documentation
# and any modifications thereto.  Any use, reproduction, disclosure or
# distribution of this software and related documentation without an express
# license agreement from NVIDIA CORPORATION is strictly prohibited.

"""Warp kernel exposed as an OmniGraph node."""

import functools
import hashlib
import importlib.util
import operator
import os
import tempfile
import traceback
from typing import (
    Any,
    Mapping,
    Sequence,
    Tuple,
    Union,
)

import numpy as np
import warp as wp

import omni.graph.core as og
import omni.timeline

from omni.warp.ogn.OgnKernelDatabase import OgnKernelDatabase
from omni.warp.scripts.kernelnode import (
    ATTR_TO_WARP_TYPE,
    MAX_DIMENSIONS,
    UserAttributeDesc,
    UserAttributesEvent,
    deserialize_user_attribute_descs,
)

QUIET_DEFAULT = wp.config.quiet

ATTR_PORT_TYPE_INPUT = og.AttributePortType.ATTRIBUTE_PORT_TYPE_INPUT
ATTR_PORT_TYPE_OUTPUT = og.AttributePortType.ATTRIBUTE_PORT_TYPE_OUTPUT

HEADER_CODE_TEMPLATE = """import warp as wp

@wp.struct
class Inputs:
{inputs}
    pass

@wp.struct
class Outputs:
{outputs}
    pass
"""

#   Internal State
# ------------------------------------------------------------------------------

def get_annotations(obj: Any) -> Mapping[str, Any]:
    """Alternative to `inspect.get_annotations()` for Python 3.9 and older."""
    # See https://docs.python.org/3/howto/annotations.html#accessing-the-annotations-dict-of-an-object-in-python-3-9-and-older
    if isinstance(obj, type):
        return obj.__dict__.get("__annotations__", {})

    return getattr(obj, "__annotations__", {})

def generate_header_code(
    attrs: Sequence[og.Attribute],
    attr_descs: Mapping[str, UserAttributeDesc],
) -> str:
    """Generates the code header based on the node's attributes."""
    # Convert all the inputs/outputs attributes into warp members.
    params = {}
    for attr in attrs:
        attr_type = attr.get_type_name()
        warp_type = ATTR_TO_WARP_TYPE.get(attr_type)

        if warp_type is None:
            raise RuntimeError(
                "Unsupported node attribute type '{}'.".format(attr_type)
            )

        params.setdefault(attr.get_port_type(), []).append(
            (
                attr.get_name().split(":")[-1],
                warp_type,
            ),
        )

    # Generate the lines of code declaring the members for each port type.
    members = {
        port_type: "\n".join("    {}: {}".format(*x) for x in items)
        for port_type, items in params.items()
    }

    # Return the template code populated with the members.
    return HEADER_CODE_TEMPLATE.format(
        inputs=members.get(ATTR_PORT_TYPE_INPUT, ""),
        outputs=members.get(ATTR_PORT_TYPE_OUTPUT, ""),
    )

def get_user_code(db: OgnKernelDatabase) -> str:
    """Retrieves the code provided by the user."""
    code_provider = db.inputs.codeProvider

    if code_provider == "embedded":
        return db.inputs.codeStr

    if code_provider == "file":
        with open(db.inputs.codeFile, "r") as f:
            return f.read()

    assert False, "Unexpected code provider '{}'.".format(code_provider)

def load_code_as_module(code: str, name: str) -> Any:
    """Loads a Python module from the given source code."""
    # It's possible to use the `exec()` built-in function to create and
    # populate a Python module with the source code defined in a string,
    # however warp requires access to the source code of the kernel's
    # function, which is only available when the original source file
    # pointed by the function attribute `__code__.co_filename` can
    # be opened to read the lines corresponding to that function.
    # As such, we must write the source code into a temporary file
    # on disk before importing it as a module and having the function
    # turned into a kernel by warp's mechanism.

    # Create a temporary file.
    file, file_path = tempfile.mkstemp(suffix=".py")

    try:
        # Save the embedded code into the temporary file.
        with os.fdopen(file, "w") as f:
            f.write(code)

        # Import the temporary file as a Python module.
        spec = importlib.util.spec_from_file_location(name, file_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        # The resulting Python module is stored into memory as a bytcode
        # object and the kernel function has already been parsed by warp
        # as long as it was correctly decorated, so it's now safe to
        # clean-up the temporary file.
        os.remove(file_path)

    return module

class InternalState:
    """Internal state for the node."""

    def __init__(self) -> None:
        self._dim_count = None
        self._code_provider = None
        self._code_str = None
        self._code_file = None
        self._code_file_timestamp = None

        self.kernel_module = None
        self.kernel_annotations = None

        self.is_valid = False

    def needs_initialization(
        self,
        db: OgnKernelDatabase,
        check_file_modified_time: bool,
    ) -> bool:
        """Checks if the internal state needs to be (re)initialized."""
        if self.is_valid:
            # If everything is in order, we only need to recompile the kernel
            # when attributes are removed, since adding new attributes is not
            # a breaking change.
            if (
                self.kernel_module is None
                or self.kernel_annotations is None
                or UserAttributesEvent.REMOVED & db.state.userAttrsEvent
            ):
                return True
        else:
            # If something previously went wrong, we always recompile the kernel
            # when attributes are edited, in case it might fix code that
            # errored out due to referencing a non-existing attribute.
            if db.state.userAttrsEvent != UserAttributesEvent.NONE:
                return True

        if self._dim_count != db.inputs.dimCount:
            return True

        if self._code_provider != db.inputs.codeProvider:
            return True

        if self._code_provider == "embedded":
            if self._code_str != db.inputs.codeStr:
                return True
        elif self._code_provider == "file":
            if (
                self._code_file != db.inputs.codeFile
                or (
                    check_file_modified_time
                    and (
                        self._code_file_timestamp
                        != os.path.getmtime(self._code_file)
                    )
                )
            ):
                return True
        else:
            assert False, (
                "Unexpected code provider '{}'.".format(self._code_provider),
            )

        return False

    def initialize(self, db: OgnKernelDatabase) -> bool:
        """Initialize the internal state and recompile the kernel."""
        # Cache the node attribute values relevant to this internal state.
        # They're the ones used to check whether this state is outdated or not.
        self._dim_count = db.inputs.dimCount
        self._code_provider = db.inputs.codeProvider
        self._code_str = db.inputs.codeStr
        self._code_file = db.inputs.codeFile

        # Retrieve the dynamic user attributes defined on the node.
        attrs = tuple(x for x in db.node.get_attributes() if x.is_dynamic())

        # Retrieve any user attribute descriptions available.
        attr_descs = deserialize_user_attribute_descs(db.state.userAttrDescs)

        # Retrieve the kernel code to evaluate.
        header_code = generate_header_code(attrs, attr_descs)
        user_code = get_user_code(db)
        code = "{}\n{}".format(header_code, user_code)

        # Create a Python module made of the kernel code.
        # We try to keep its name unique to ensure that it's not clashing with
        # other kernel modules from the same session.
        uid = hashlib.blake2b(bytes(code, encoding="utf-8"), digest_size=8)
        module_name = "warp-kernelnode-{}".format(uid.hexdigest())
        kernel_module = load_code_as_module(code, module_name)

        # Validate the module's contents.
        if not hasattr(kernel_module, "compute"):
            db.log_error(
                "The code must define a kernel function named 'compute'."
            )
            return False
        if not isinstance(kernel_module.compute, wp.context.Kernel):
            db.log_error(
                "The 'compute' function must be decorated with '@wp.kernel'."
            )
            return False

        # Retrieves the type annotations for warp's kernel in/out structures.
        kernel_annotations = {
            ATTR_PORT_TYPE_INPUT: get_annotations(kernel_module.Inputs.cls),
            ATTR_PORT_TYPE_OUTPUT: get_annotations(kernel_module.Outputs.cls),
        }

        # Assert that our code is doing the right thing—each annotation found
        # must map onto a corresponding node attribute.
        assert all(
            (
                sorted(annotations.keys())
                == sorted(
                    x.get_name().split(":")[-1] for x in attrs
                    if x.get_port_type() == port_type
                )
            )
            for port_type, annotations in kernel_annotations.items()
        )

        # Ensure that all output parameters are arrays. Writing to non-array
        # types is not supported as per CUDA's design.
        invalid_attrs = tuple(
            k
            for k, v in kernel_annotations[ATTR_PORT_TYPE_OUTPUT].items()
            if not isinstance(v, wp.array)
        )
        if invalid_attrs:
            db.log_error(
                "Output attributes are required to be arrays but "
                "the following attributes are not: {}."
                .format(", ".join(invalid_attrs))
            )
            return False

        # Configure warp to only compute the forward pass.
        wp.set_module_options({"enable_backward": False}, module=kernel_module)

        # Store the public members.
        self.kernel_module = kernel_module
        self.kernel_annotations = kernel_annotations

        return True

#   Compute
# ------------------------------------------------------------------------------

def cast_array_to_warp_type(
    value: Union[np.array, og.DataWrapper],
    warp_annotation: Any,
    device: wp.context.Device,
) -> wp.array:
    """Casts an attribute array to its corresponding warp type."""
    if device.is_cpu:
        return wp.array(
            value,
            dtype=warp_annotation.dtype,
            owner=False,
        )

    elif device.is_cuda:
        return omni.warp.from_omni_graph(
            value,
            dtype=warp_annotation.dtype,
        )

    assert False, "Unexpected device '{}'.".format(device.alias)

def are_array_annotations_equal(
    annotation_1: Any,
    annotation_2: Any,
) -> bool:
    """Checks whether two array annotations are equal."""
    assert isinstance(annotation_1, wp.array)
    assert isinstance(annotation_2, wp.array)
    return (
        annotation_1.dtype == annotation_2.dtype
        and annotation_1.ndim == annotation_2.ndim
    )

def get_kernel_args(
    db: OgnKernelDatabase,
    module: Any,
    annotations: Mapping[og.AttributePortType, Sequence[Tuple[str, Any]]],
    dims: Sequence[int],
    device: wp.context.Device,
) -> Tuple[Any, Any]:
    """Retrieves the in/out argument values to pass to the kernel."""
    # Initialize the kernel's input data.
    inputs = module.Inputs()
    for name, warp_annotation in annotations[ATTR_PORT_TYPE_INPUT].items():
        # Retrieve the input attribute value and cast it to the corresponding
        # warp type if is is an array.
        value = getattr(db.inputs, name)
        if isinstance(warp_annotation, wp.array):
            value = cast_array_to_warp_type(value, warp_annotation, device)

        # Store the result in the inputs struct.
        setattr(inputs, name, value)

    # Initialize the kernel's output data.
    outputs = module.Outputs()
    for name, warp_annotation in annotations[ATTR_PORT_TYPE_OUTPUT].items():
        assert isinstance(warp_annotation, wp.array)

        # Retrieve the size of the array to allocate.
        ref_annotation = annotations[ATTR_PORT_TYPE_INPUT].get(name)
        if (
            isinstance(ref_annotation, wp.array)
            and are_array_annotations_equal(warp_annotation, ref_annotation)
        ):
            # If there's an existing input with the same name and type,
            # we allocate a new array matching the input's length.
            size = len(getattr(inputs, name))
        else:
            # Fallback to allocate an array matching the kernel's dimensions.
            size = functools.reduce(operator.mul, dims)

        # Allocate the array.
        setattr(db.outputs, "{}_size".format(name), size)

        # Retrieve the output attribute value and cast it to the corresponding
        # warp type.
        value = getattr(db.outputs, name)
        value = cast_array_to_warp_type(value, warp_annotation, device)

        # Store the result in the outputs struct.
        setattr(outputs, name, value)

    return (inputs, outputs)

def write_output_attrs(
    db: OgnKernelDatabase,
    annotations: Mapping[og.AttributePortType, Sequence[Tuple[str, Any]]],
    outputs: Any,
    device: wp.context.Device,
) -> None:
    """Writes the output values to the node's attributes."""
    if device.is_cuda:
        # CUDA attribute arrays are directly being written to by Warp.
        return

    for name, warp_annotation in annotations[ATTR_PORT_TYPE_OUTPUT].items():
        assert isinstance(warp_annotation, wp.array)

        value = getattr(outputs, name)
        setattr(db.outputs, name, value)

def compute(db: OgnKernelDatabase, device: wp.context.Device) -> None:
    """Evaluates the node."""
    db.set_dynamic_attribute_memory_location(
        on_gpu=device.is_cuda,
        gpu_ptr_kind=og.PtrToPtrKind.CPU,
    )

    # Ensure that our internal state is correctly initialized.
    timeline =  omni.timeline.get_timeline_interface()
    if db.internal_state.needs_initialization(db, timeline.is_stopped()):
        if not db.internal_state.initialize(db):
            return

        db.internal_state.is_valid = True

    # Exit early if there are no outputs.
    if not db.internal_state.kernel_annotations[ATTR_PORT_TYPE_OUTPUT]:
        return

    # Retrieve the number of dimensions.
    dim_count = min(max(db.inputs.dimCount, 1), MAX_DIMENSIONS)

    # Retrieve the shape of the dimensions to launch the kernel with.
    dims = tuple(
        max(getattr(db.inputs, "dim{}".format(i + 1)), 0)
        for i in range(dim_count)
    )

    # Retrieve the inputs and outputs argument values to pass to the kernel.
    inputs, outputs = get_kernel_args(
        db,
        db.internal_state.kernel_module,
        db.internal_state.kernel_annotations,
        dims,
        device,
    )

    # Ensure that all array input attributes are not NULL, unless they are set
    # as being optional.
    # Note that adding a new non-optional array attribute might still cause
    # the compute to succeed since the kernel recompilation is delayed until
    # `InternalState.needs_initialization()` requests it, meaning that the new
    # attribute won't show up as a kernel annotation just yet.
    for attr_name in db.internal_state.kernel_annotations[ATTR_PORT_TYPE_INPUT]:
        value = getattr(inputs, attr_name)
        if not isinstance(value, wp.array):
            continue

        attr = og.Controller.attribute("inputs:{}".format(attr_name), db.node)
        if not attr.is_optional_for_compute and not value.ptr:
            raise RuntimeError(
                "Empty value for non-optional attribute 'inputs:{}'."
                .format(attr_name)
            )

    # Launch the kernel.
    wp.launch(
        db.internal_state.kernel_module.compute,
        dim=dims,
        inputs=[inputs],
        outputs=[outputs],
    )

    # Write the output values to the node's attributes.
    write_output_attrs(
        db,
        db.internal_state.kernel_annotations,
        outputs,
        device,
    )

#   Node Entry Point
# ------------------------------------------------------------------------------

class OgnKernel:
    """Warp's kernel node."""

    @staticmethod
    def internal_state() -> InternalState:
        return InternalState()

    @staticmethod
    def initialize(graph_context: og.GraphContext, node: og.Node) -> None:
        # Populate the devices tokens.
        attr = og.Controller.attribute("inputs:device", node)
        if attr.get_metadata(og.MetadataKeys.ALLOWED_TOKENS) is None:
            attr.set_metadata(
                og.MetadataKeys.ALLOWED_TOKENS,
                ",".join(["cpu", "cuda:0"])
            )

    @staticmethod
    def compute(db: OgnKernelDatabase) -> None:
        try:
            device = wp.get_device(db.inputs.device)
        except Exception:
            # Fallback to a default device.
            # This can happen due to a scene being authored on a device
            # (e.g.: `cuda:1`) that is not available to another user opening
            # that same scene.
            device = wp.get_device("cuda:0")

        try:
            with wp.ScopedDevice(device):
                compute(db, device)
        except Exception:
            db.internal_state.is_valid = False
            db.log_error(traceback.format_exc())
            wp.config.quiet = True
            return
        else:
            wp.config.quiet = QUIET_DEFAULT

        # Reset the user attributes event since it has now been processed.
        db.state.userAttrsEvent = UserAttributesEvent.NONE

        # Fire the execution for the downstream nodes.
        db.outputs.execOut = og.ExecutionAttributeState.ENABLED
