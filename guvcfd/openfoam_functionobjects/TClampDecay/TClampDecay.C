#include "TClampDecay.H"
#include "addToRunTimeSelectionTable.H"

// * * * * * * * * * * * * * * Static Data Members * * * * * * * * * * * * //

namespace Foam
{
namespace functionObjects
{
    defineTypeNameAndDebug(TClampDecay, 0);
    addToRunTimeSelectionTable(functionObject, TClampDecay, dictionary);
}
}


// * * * * * * * * * * * * Private Member Functions * * * * * * * * * * * //

void Foam::functionObjects::TClampDecay::readKUV()
{
    IOobject kUVHeader
    (
        "kUV",
        "0",
        mesh_,
        IOobject::MUST_READ,
        IOobject::NO_WRITE,
        false
    );

    if (!kUVHeader.typeHeaderOk<volScalarField>(true))
    {
        FatalErrorInFunction
            << "TClampDecay: could not find a 'kUV' field in the '0' "
            << "time directory of case " << mesh_.time().path()
            << exit(FatalError);
    }

    kUV_.reset(new volScalarField(kUVHeader, mesh_));
}


// * * * * * * * * * * * * * * * * Constructor  * * * * * * * * * * * * * //

Foam::functionObjects::TClampDecay::TClampDecay
(
    const word& name,
    const Time& runTime,
    const dictionary& dict
)
:
    fvMeshFunctionObject(name, runTime, dict),
    fieldName_("T"),
    Tmax_(0)
{
    read(dict);
    readKUV();
}


// * * * * * * * * * * * * * * Member Functions  * * * * * * * * * * * * //

bool Foam::functionObjects::TClampDecay::read(const dictionary& dict)
{
    fvMeshFunctionObject::read(dict);
    dict.readIfPresent("field", fieldName_);
    dict.readEntry("Tmax", Tmax_);
    return true;
}


bool Foam::functionObjects::TClampDecay::execute()
{
    auto* Tptr = mesh_.getObjectPtr<volScalarField>(fieldName_);
    if (!Tptr)
    {
        return true;
    }

    volScalarField& T = *Tptr;
    const volScalarField& kUV = kUV_();
    const scalar dt = mesh_.time().deltaTValue();

    label nClamped = 0;
    forAll(T, celli)
    {
        if (T[celli] < 0)
        {
            // exp(-kUV*dt) is always positive, so decaying a negative
            // value can only ever produce another negative-or-zero value
            // - a decay step here is mathematically pointless, always
            // degenerating to a plain floor. Written directly instead of
            // routing through the (irrelevant for this branch) exp() math.
            T[celli] = 0;
            ++nClamped;
        }
        else if (T[celli] > Tmax_)
        {
            const scalar decayed = T[celli]*exp(-kUV[celli]*dt);
            T[celli] = min(decayed, Tmax_);
            ++nClamped;
        }
    }

    if (nClamped > 0)
    {
        T.correctBoundaryConditions();
        Info<< "    TClampDecay: corrected " << nClamped
            << " cell(s) of " << fieldName_
            << " outside [0, " << Tmax_ << "]" << endl;
    }

    return true;
}


bool Foam::functionObjects::TClampDecay::write()
{
    return true;
}
