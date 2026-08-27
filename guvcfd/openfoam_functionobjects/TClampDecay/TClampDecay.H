#ifndef functionObjects_TClampDecay_H
#define functionObjects_TClampDecay_H

#include "fvMeshFunctionObject.H"
#include "volFields.H"

namespace Foam
{
namespace functionObjects
{

/*---------------------------------------------------------------------------*\
                       Class TClampDecay Declaration
\*---------------------------------------------------------------------------*/

// Replaces an out-of-[0,Tmax] cell with a locally sink-decayed value
// (T*exp(-kUV*dt), then clamped) instead of a hard reset to a boundary -
// see execute() for why. Written for GUV-CFD's UV-decay outer-iteration
// divergence: at very high local kUV, the outer SIMPLE loop's own
// coupling (relaxation-lag on the ddt "old time" reference, amplified by
// div(phi,T)'s linearUpwind deferred-correction term) can drive a single
// cell to an unbounded floating-point blowup, or below zero, within a
// handful of iterations - see ANALYSIS_LOG.md.
class TClampDecay
:
    public fvMeshFunctionObject
{
    // Private Data

        //- Name of the field to correct (this project only ever uses "T")
        word fieldName_;

        //- Upper physical bound
        scalar Tmax_;

        //- Cell-wise UV inactivation rate, read once from this case's own
        //  static 0/kUV - never registered in the objectRegistry, since
        //  nothing else needs to look it up, and a private copy sidesteps
        //  any question of whether a live-registered "kUV" would survive
        //  a chunked solver restart (kUV is baked into fvOptions as plain
        //  numeric Sp coefficients, never read live by the solver itself)
        autoPtr<volScalarField> kUV_;


    // Private Member Functions

        void readKUV();


public:

    //- Runtime type information
    TypeName("TClampDecay");


    // Constructors

        TClampDecay
        (
            const word& name,
            const Time& runTime,
            const dictionary& dict
        );

        //- No copy construct
        TClampDecay(const TClampDecay&) = delete;

        //- No copy assignment
        void operator=(const TClampDecay&) = delete;


    //- Destructor
    virtual ~TClampDecay() = default;


    // Member Functions

        virtual bool read(const dictionary& dict);
        virtual bool execute();
        virtual bool write();
};

} // End namespace functionObjects
} // End namespace Foam

#endif
